# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.command_security

TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.

命令白名单校验编排入口。

对外提供 ``validate_command`` / ``is_command_allowed`` 及 ``validate_path``
等公开接口，编排 parser 的解析能力与 whitelist 的数据表，
完成单条命令的递归校验。保持原 ``command_security`` 模块的对外签名不变。

输出脱敏**不由本模块提供，也不在本模块触发**：唯一真源在 ``redaction.operations``
（``KNOWN_VALUES_PLACEHOLDER`` / ``redact_known_values``），由 core provider 侧在拼接
工具返回值时调用。本模块（命令编排）与脱敏彻底解耦。
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

import bashlex

from aidev_agent.packages.security.command.command_approval import (
    get_command_approval_mode,
    require_command_approval,
)
from aidev_agent.packages.security.command.command_parser import (
    _check_rejected_patterns,
    _check_script_path_allowed,
    _extract_bash_c_content,
    _extract_command_parts,
    _extract_script_argument,
    _normalize_command_name,
    _split_by_shell_operators,
)
from aidev_agent.packages.security.command.command_whitelist import (
    ALLOWED_COMMANDS,
    DEFAULT_ALLOWED_SCRIPT_DIRS,
    EFFECTIVE_ALLOWED_COMMANDS,
    PARAMETER_RESTRICTIONS,
    AllowedFlagsOnly,
    ForbiddenFlags,
    ParameterRestriction,
)
from aidev_agent.packages.security.command.dangerous_commands import scan_dangerous_commands
from aidev_agent.packages.security.command.validation_result import ValidationResult
from aidev_agent.pydantic_models import SecuritySettings

logger = logging.getLogger(__name__)

# ========== 单条命令校验 ==========


def _validate_single_command(
    sub_cmd: str,
    allowed_dirs: list[str],
    recursion_depth: int = 0,
    visited: set[str] | None = None,
) -> ValidationResult:
    """校验单条命令（可能是 bash -c 形式或普通命令）。

    这是递归校验的核心函数。

    Args:
        sub_cmd: 子命令字符串。
        allowed_dirs: 允许的脚本目录列表。
        recursion_depth: 递归深度（防止无限递归）。
        visited: 已访问的命令集合（防止循环）。

    Returns:
        校验结果。
    """
    if visited is None:
        visited = set()

    # 防止无限递归
    if recursion_depth > 10:
        return ValidationResult(
            is_allowed=False,
            reason="命令嵌套层级过深（超过 10 层），可能存在递归攻击",
            rejected_command=sub_cmd,
        )

    # 防止循环
    cmd_key = sub_cmd.strip()
    if cmd_key in visited:
        return ValidationResult(
            is_allowed=False,
            reason=f"检测到循环命令引用: {sub_cmd}",
            rejected_command=sub_cmd,
        )
    visited = visited | {cmd_key}

    # Step 1: 去除首尾空白
    sub_cmd = sub_cmd.strip()
    if not sub_cmd:
        return ValidationResult(
            is_allowed=False,
            reason="空命令",
            rejected_command=sub_cmd,
        )

    # Step 2: 检测注释（# 开头的命令或纯注释）
    if sub_cmd.startswith("#"):
        return ValidationResult(
            is_allowed=False,
            reason="注释不是有效命令",
            rejected_command=sub_cmd,
        )

    # Step 3: 检测禁止模式（命令替换、进程替换、后台执行、重定向等）
    ok, reason = _check_rejected_patterns(sub_cmd)
    if not ok:
        return ValidationResult(
            is_allowed=False,
            reason=reason,
            rejected_command=sub_cmd,
            rejection_category="forbidden_pattern",
        )

    # Step 4: 使用 bashlex 解析命令
    try:
        nodes = bashlex.parse(sub_cmd)
    except bashlex.errors.ParsingError as e:
        return ValidationResult(
            is_allowed=False,
            reason=f"命令语法错误（可能是引号不匹配）: {e}",
            rejected_command=sub_cmd,
        )

    if not nodes:
        return ValidationResult(
            is_allowed=False,
            reason="无法解析命令",
            rejected_command=sub_cmd,
        )

    # Step 5: 处理多节点情况或包含操作符/pipeline 的命令
    # 如果有多个顶层节点、存在操作符、或是 pipeline，需要递归拆分
    needs_splitting = False
    for node in nodes:
        kind = getattr(node, "kind", None)
        if kind == "pipeline":
            needs_splitting = True
            break
        if kind == "list":
            for part in node.parts:
                if getattr(part, "kind", None) == "operator":
                    needs_splitting = True
                    break
        if needs_splitting:
            break

    if len(nodes) > 1 or needs_splitting:
        # 需要按操作符拆分
        try:
            sub_commands = _split_by_shell_operators(sub_cmd)
        except ValueError as e:
            return ValidationResult(
                is_allowed=False,
                reason=str(e),
                rejected_command=sub_cmd,
            )

        # 如果拆分后只有一条命令且和原命令相同，继续校验该命令
        if len(sub_commands) == 1 and sub_commands[0] == sub_cmd:
            pass  # 继续下面的单命令校验
        else:
            # 递归校验每个子命令
            for sc in sub_commands:
                sc = sc.strip()
                if not sc:
                    continue
                # 先检测注释
                if sc.startswith("#"):
                    continue
                result = _validate_single_command(
                    sc,
                    allowed_dirs,
                    recursion_depth + 1,
                    visited,
                )
                if not result.is_allowed:
                    return result
            return ValidationResult(is_allowed=True)

    # Step 6: 提取命令名和参数
    # 找到第一个 command 节点
    cmd_node = None
    for node in nodes:
        if getattr(node, "kind", None) == "command":
            cmd_node = node
            break

    if cmd_node is None:
        # 尝试从 list 节点中提取
        for node in nodes:
            if getattr(node, "kind", None) == "list":
                for part in node.parts:
                    if getattr(part, "kind", None) == "command":
                        cmd_node = part
                        break
                if cmd_node:
                    break

    if cmd_node is None:
        return ValidationResult(
            is_allowed=False,
            reason="无法识别命令结构",
            rejected_command=sub_cmd,
        )

    try:
        raw_cmd_name, args = _extract_command_parts(cmd_node)
    except Exception as e:
        return ValidationResult(
            is_allowed=False,
            reason=f"命令解析失败: {e}",
            rejected_command=sub_cmd,
        )

    if not raw_cmd_name:
        return ValidationResult(
            is_allowed=False,
            reason="无法提取命令名",
            rejected_command=sub_cmd,
        )

    # Step 7: 规范化命令名
    try:
        cmd_name = _normalize_command_name(raw_cmd_name)
    except ValueError as e:
        return ValidationResult(
            is_allowed=False,
            reason=str(e),
            rejected_command=sub_cmd,
        )

    # Step 8: 查白名单
    if cmd_name not in EFFECTIVE_ALLOWED_COMMANDS:
        return ValidationResult(
            is_allowed=False,
            reason=f"命令 '{cmd_name}' 不在允许列表中，如确需使用请联系管理员添加",
            rejected_command=sub_cmd,
            rejection_category="not_in_whitelist",
        )

    # Step 9: 检查参数限制
    if cmd_name in PARAMETER_RESTRICTIONS:
        restriction = PARAMETER_RESTRICTIONS[cmd_name]
        ok, reason = restriction.is_allowed(args)
        if not ok:
            return ValidationResult(
                is_allowed=False,
                reason=f"命令 '{cmd_name}' {reason}",
                rejected_command=sub_cmd,
            )

    # Step 10: 特殊命令额外检查

    # Python / Python3：放行所有形式（脚本文件、-m module、-c 内联代码等）
    # （暂时 不再限制 -c 参数， 不要移除注释）
    # if cmd_name in ("python", "python3"):
    #     if "-c" in args:
    #         return ValidationResult(
    #             is_allowed=False,
    #             reason=f"命令 '{cmd_name} -c' 不允许执行内联代码",
    #             rejected_command=sub_cmd,
    #         )

    # Bash / Sh / Zsh：处理 -c 递归和脚本路径
    elif cmd_name in ("bash", "sh", "zsh"):
        bash_c_content = _extract_bash_c_content(cmd_name, args)
        if bash_c_content is not None:
            # 递归校验 bash -c 内部命令
            logger.debug("递归校验 bash -c 内容: %s", bash_c_content)
            result = _validate_single_command(
                bash_c_content,
                allowed_dirs,
                recursion_depth + 1,
                visited,
            )
            if not result.is_allowed:
                # 内部命令已被拒绝，直接返回其拒绝原因（避免重复前缀）
                return ValidationResult(
                    is_allowed=False,
                    reason=result.reason,
                    rejected_command=f"{cmd_name} -c '{bash_c_content}'",
                )
        elif "-c" in args:
            # bash -c 但缺少要执行的命令内容
            return ValidationResult(
                is_allowed=False,
                reason=f"命令 '{cmd_name} -c' 缺少要执行的命令内容",
                rejected_command=sub_cmd,
            )
        else:
            # 直接执行脚本文件
            script_path = _extract_script_argument(args)
            if script_path:
                ok, reason = _check_script_path_allowed(script_path, allowed_dirs)
                if not ok:
                    return ValidationResult(
                        is_allowed=False,
                        reason=f"Shell 脚本{reason}",
                        rejected_command=sub_cmd,
                    )

    return ValidationResult(is_allowed=True)


# ========== 路径验证 ==========


def validate_path(path: str, *, allowed_prefixes: list[str] | None = None) -> str:
    r"""验证并规范化文件路径以确保安全。

    通过防止目录遍历攻击和强制一致格式来确保路径安全可用。
    所有路径都会被规范化为使用正斜杠并以前导斜杠开头。

    此函数设计用于虚拟文件系统路径，会拒绝 Windows 绝对路径
    （如 C:/...、F:/...）以保持一致性并防止路径格式歧义。

    Args:
        path: 要验证和规范化的路径
        allowed_prefixes: 可选的允许路径前缀列表。如果提供，
            规范化后的路径必须以其中一个前缀开头

    Returns:
        规范化的标准路径，避免 a/../../b 这种情况出现

    Raises:
        ValueError: 当路径包含遍历序列（`..`）、
            是 Windows 绝对路径（如 C:/...）、或不以允许的前缀开头时抛出
    """

    # 拒绝 Windows 绝对路径（如 C:\...、D:/...）
    if re.match(r"^[a-zA-Z]:", path):
        msg = (
            f"Windows absolute paths are not supported: {path}. "
            "Please use virtual paths starting with / (e.g., /workspace/file.txt)"
        )
        raise ValueError(msg)

    normalized = os.path.normpath(path)
    normalized = normalized.replace("\\", "/")

    # 先规范化再检查路径遍历，避免 a/../b 被误拒（normpath 后为安全的 b）
    if ".." in normalized.split("/"):
        msg = f"Path traversal not allowed: {path}"
        raise ValueError(msg)

    if allowed_prefixes is not None and not any(normalized.startswith(prefix) for prefix in allowed_prefixes):
        msg = f"Path must start with one of {allowed_prefixes}: {path}"
        raise ValueError(msg)

    return normalized


# ========== 公开接口 ==========


def validate_command(
    command: str,
    *,
    allowed_script_dirs: list[str] | None = None,
    strict: bool = True,
) -> ValidationResult:
    """校验命令是否允许执行。

    对输入的命令字符串进行完整的白名单校验，包括：
    - 禁止模式检测（命令替换、进程替换、后台执行等）
    - 按 shell 操作符拆分并递归校验子命令
    - 命令名白名单检查
    - 参数限制检查
    - 特殊命令（python、bash 等）额外检查
    - bash -c 内部命令递归校验

    Args:
        command: 待校验的命令字符串。
        allowed_script_dirs: 允许执行脚本的目录列表。
            默认为 DEFAULT_ALLOWED_SCRIPT_DIRS（可从环境变量配置）。
        strict: 严格模式。当前保留参数，未来可能用于放宽限制。

    Returns:
        ValidationResult: 包含 is_allowed 和 reason 的校验结果。

    Example:
        >>> result = validate_command("ls /tmp")
        >>> result.is_allowed
        True
        >>> result = validate_command("rm file.txt")
        >>> result.is_allowed
        False
        >>> result.reason
        "命令 'rm' 不在允许列表中..."
    """
    if allowed_script_dirs is None:
        allowed_script_dirs = DEFAULT_ALLOWED_SCRIPT_DIRS

    logger.debug("开始校验命令: %s", command)

    # 预处理：去除首尾空白，检测空字节
    command = command.strip()
    if not command:
        return ValidationResult(
            is_allowed=False,
            reason="空命令",
            rejected_command=command,
        )

    # 检测空字节（安全攻击常用）
    if "\x00" in command:
        return ValidationResult(
            is_allowed=False,
            reason="命令中包含空字节，不允许执行",
            rejected_command=command,
        )

    # 检测整条命令的注释
    if command.startswith("#"):
        return ValidationResult(
            is_allowed=False,
            reason="注释不是有效命令",
            rejected_command=command,
        )

    # 先检测整条命令的禁止模式
    ok, reason = _check_rejected_patterns(command)
    if not ok:
        logger.warning("命令被拒绝（禁止模式）: %s, 原因: %s", command, reason)
        return ValidationResult(
            is_allowed=False,
            reason=reason,
            rejected_command=command,
            rejection_category="forbidden_pattern",
        )

    # 进入单命令递归校验
    result = _validate_single_command(command, allowed_script_dirs)

    if result.is_allowed:
        logger.debug("命令通过校验: %s", command)
    else:
        logger.warning(
            "命令被拒绝: %s, 原因: %s, 被拒命令: %s",
            command,
            result.reason,
            result.rejected_command or command,
        )

    return result


def is_command_allowed(
    command: str,
    *,
    allowed_script_dirs: list[str] | None = None,
    strict: bool = True,
) -> bool:
    """快捷方法：返回命令是否允许执行。

    Args:
        command: 待校验的命令字符串。
        allowed_script_dirs: 允许执行脚本的目录列表。
        strict: 严格模式。

    Returns:
        命令是否允许。

    Example:
        >>> is_command_allowed("ls /tmp")
        True
        >>> is_command_allowed("rm file.txt")
        False
    """
    result = validate_command(
        command,
        allowed_script_dirs=allowed_script_dirs,
        strict=strict,
    )
    return result.is_allowed


# ========== 空值兜底 ==========

ENSURE_NON_EMPTY_HINT = "[harness]该指令没有输出，可能是因为沙箱未能正确执行或者命令没有输出，请重试或者使用其他命令"


def ensure_non_empty(value: str) -> str:
    """确保返回值非空，空字符串时返回友好提示。

    从 ``core.tools.runtime_tools.provider`` 下沉到本包，供命令安全编排与
    沙箱工具层共用；core 层保留同名薄包装委托到本函数，避免重复实现。
    """
    if not value or not value.strip():
        return ENSURE_NON_EMPTY_HINT
    return value


# ========== 命令安全编排（三层防护）==========

# 说明：命令拒绝文案为静态措辞 + 命令/原因，不含 backend 注入的敏感值
# （敏感值只随沙箱输出泄漏，输出脱敏在 core provider 侧完成），
# 故本模块不引用 redaction —— 命令编排与脱敏彻底解耦。


def enforce_command_security(
    command: str,
    target_runtime: str,
    security_settings: SecuritySettings | None = None,
    risk_assessor: Any | None = None,
) -> None:
    """执行命令前进行三层安全防护（黑名单 → 白名单 → 命令级审批）。

    - Layer 1：危险命令黑名单（``.dangerous_commands``）——命中即硬拒绝，
      不可审批、不可放行；
    - Layer 0：命令白名单（``validate_command``）——默认拒绝；
    - Layer 2：命令级审批（``.command_approval``）——仅「不在白名单」的灰名单
      命令（``rejection_category == "not_in_whitelist"``）且启用审批时，按
      ``command_approval_mode`` 分流：
        * ``smart`` 且注入 ``risk_assessor`` 时，先由辅助 LLM 三级预分流
          （low 放行 / high 拒绝 / uncertain 走 ITSM）；
        * 其余（``manual`` / 未注入评估器）直接走 ITSM HITL。
      审批通过放行，拒绝 / 未启用则维持默认拒绝。

    拒绝一律抛 ``ValueError``（fail-closed），放行时正常返回。

    拒绝文案不含脱敏处理：文案为静态措辞 + 命令/原因，不可能包含 backend
    注入的敏感值（那些值只随沙箱输出泄漏，脱敏在 core provider 侧完成）。

    Args:
        command: 模型生成的待执行 shell 命令。
        target_runtime: 目标运行时标识（写入审批单，便于审计）。
        security_settings: 安全防护配置。``None`` 表示未启用黑名单与命令级审批
            （仅走白名单 Layer 0）。
        risk_assessor: 命令风险评估器（smart 审批模式）。None 时 smart 模式
            自动回落为 manual（全量 ITSM 审批）。
    """
    # Layer 1：危险命令黑名单（硬拒绝，不可审批）
    if security_settings is not None and security_settings.command_blacklist:
        hits = scan_dangerous_commands(command)
        if hits:
            detail = "; ".join(f"{hit.label}({hit.category})" for hit in hits)
            raise ValueError(ensure_non_empty(f"命令执行被拒绝（危险命令黑名单）：{detail}"))

    # Layer 0：命令白名单（默认拒绝）
    result = validate_command(command)
    if result.is_allowed:
        return

    # Layer 2：命令级审批（仅「不在白名单」的灰名单命令可审批）
    if (
        result.rejection_category == "not_in_whitelist"
        and security_settings is not None
        and security_settings.command_approval
    ):
        # smart 模式：辅助 LLM 三级预分流（low 放行 / high 拒绝 / uncertain 走 ITSM）
        if get_command_approval_mode(security_settings) == "smart" and risk_assessor is not None:
            risk = risk_assessor.assess(command)
            if risk == "low":
                return
            if risk == "high":
                raise ValueError(ensure_non_empty("命令执行被拒绝（风险评估：high）。"))
            # uncertain 落到 ITSM 审批
        if require_command_approval(command, target_runtime=target_runtime, security_settings=security_settings):
            return
        raise ValueError(ensure_non_empty("命令审批未通过，已取消执行。"))

    # 其余拒绝（禁止模式 / 空命令 / 空字节 / 嵌套过深等）一律硬拒绝
    raise ValueError(ensure_non_empty(f"命令执行被拒绝：{result.reason}"))


# ========== 导出 ==========

__all__ = [
    "ValidationResult",
    "ParameterRestriction",
    "AllowedFlagsOnly",
    "ForbiddenFlags",
    "ALLOWED_COMMANDS",
    "PARAMETER_RESTRICTIONS",
    "EFFECTIVE_ALLOWED_COMMANDS",
    "DEFAULT_ALLOWED_SCRIPT_DIRS",
    "validate_command",
    "is_command_allowed",
    "validate_path",
    "enforce_command_security",
    "ensure_non_empty",
    "ENSURE_NON_EMPTY_HINT",
    "_normalize_command_name",
]
