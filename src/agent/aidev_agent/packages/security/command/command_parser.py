# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.command_parser

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

bash 命令语法解析与拆分。

基于 bashlex 提供命令名规范化、bash -c 内容提取、
按 shell 操作符拆分、禁止模式检测与脚本路径校验等解析能力。
"""

from __future__ import annotations

import os
import re
from typing import Any

import bashlex
import bashlex.ast as bashast

from aidev_agent.packages.security.command.command_whitelist import (
    _BRACE_EXPANSION_PATTERN,
    _COMMAND_SUBSTITUTION_PATTERN,
    _REDIRECT_PATTERN,
    _REDIRECT_TO_DEV_NULL_PATTERN,
)

# ========== 命令名规范化 ==========


def _normalize_command_name(raw_name: str) -> str:
    """规范化命令名：提取 basename、拒绝路径遍历、处理特殊形式。

    Args:
        raw_name: 原始命令名（可能包含路径）。

    Returns:
        规范化后的纯命令名。

    Raises:
        ValueError: 当命令名包含路径遍历或其他不安全模式时。
    """
    name = raw_name.strip()

    if not name:
        raise ValueError("空命令名")

    # 拒绝包含路径遍历的命令
    if ".." in name:
        raise ValueError(f"命令名包含路径遍历: {raw_name}")

    # 如果是绝对路径，提取 basename
    if name.startswith("/"):
        normalized = os.path.normpath(name)
        if ".." in normalized:
            raise ValueError(f"命令路径包含遍历: {raw_name}")
        return os.path.basename(normalized)

    # 如果是相对路径（以 ./ 或 ../ 开头）
    if name.startswith(("./", "../")) or ("/" in name and not name.startswith("-")):
        normalized = os.path.normpath(name)
        if ".." in normalized:
            raise ValueError(f"命令路径包含遍历: {raw_name}")
        return os.path.basename(normalized)

    # 纯命令名，直接返回
    return name


# ========== bashlex 解析辅助 ==========


def _extract_command_parts(node: Any) -> tuple[str, list[str]]:
    """从 bashlex CommandNode 中提取命令名和参数列表。

    正确处理引号、转义等 shell 语法，提取字面值。

    Args:
        node: bashlex CommandNode。

    Returns:
        (命令名, 参数列表) 元组。
    """
    parts: list[str] = []
    cmd_name: str = ""

    for child in node.parts:
        word = _node_to_string(child) if isinstance(child, bashast.node) else str(child)

        if not cmd_name:
            cmd_name = word
        else:
            parts.append(word)

    return cmd_name, parts


def _node_to_string(node: Any) -> str:
    """将 bashlex AST 节点转换为字符串字面值。

    处理 WordNode、ParameterNode 等节点类型，正确展开
    引号内的内容和变量引用。

    Args:
        node: bashlex AST 节点。

    Returns:
        节点的字符串字面值。
    """
    kind = getattr(node, "kind", None)

    if kind == "word":
        # WordNode：可能包含引号、转义等
        # 需要提取其中的 word 属性或 parts
        if hasattr(node, "word"):
            return node.word
        if hasattr(node, "parts") and node.parts:
            return "".join(_part_to_string(p) for p in node.parts)
        return str(node)

    elif kind == "parameter":
        # 变量引用如 $HOME、${VAR}
        if hasattr(node, "name"):
            name = node.name
            return f"${{{name}}}"
        return str(node)

    elif kind == "commandsubstitution":
        # 命令替换 $(cmd) 或 `cmd`
        return "$(cmd)"

    elif hasattr(node, "word"):
        return node.word

    elif hasattr(node, "parts") and node.parts:
        return "".join(_part_to_string(p) for p in node.parts)

    return str(node)


def _part_to_string(part: Any) -> str:
    """将 bashlex 节点的一部分转换为字符串。"""
    if isinstance(part, str):
        return part
    if hasattr(part, "word"):
        return part.word
    if hasattr(part, "parts") and part.parts:
        return "".join(_part_to_string(p) for p in part.parts)
    return str(part)


# ========== 命令拆分 ==========


def _split_by_shell_operators(command: str) -> list[str]:
    """使用 bashlex 将命令按 shell 操作符拆分为子命令列表。

    正确处理引号内的操作符（不作为分隔符）。
    使用 AST 节点的 pos 属性从原始命令字符串中切片，保留引号等原始语法。

    Args:
        command: 待拆分的命令字符串。

    Returns:
        子命令字符串列表。空子命令会被过滤。
    """
    sub_commands: list[str] = []

    try:
        nodes = bashlex.parse(command)
    except bashlex.errors.ParsingError as e:
        # 解析失败，可能是语法错误或引号不匹配
        raise ValueError(f"命令解析失败: {e}")

    for node in nodes:
        kind = getattr(node, "kind", None)

        if kind == "list":
            # list 节点包含多个通过操作符连接的命令
            current_parts: list[Any] = []
            for part in node.parts:
                part_kind = getattr(part, "kind", None)
                if part_kind == "operator":
                    # 遇到操作符，保存当前积累的命令
                    cmd_str = _nodes_to_command_string(current_parts, command)
                    if cmd_str.strip():
                        sub_commands.append(cmd_str.strip())
                    current_parts = []
                elif part_kind == "command" or part_kind == "pipe":
                    current_parts.append(part)
                else:
                    current_parts.append(part)
            # 保存最后积累的命令
            if current_parts:
                cmd_str = _nodes_to_command_string(current_parts, command)
                if cmd_str.strip():
                    sub_commands.append(cmd_str.strip())

        elif kind == "command":
            # 单个命令
            cmd_str = _node_to_original_string(node, command)
            if cmd_str.strip():
                sub_commands.append(cmd_str.strip())

        elif kind == "pipeline":
            # 管道命令：拆分为多个子命令
            for part in node.parts:
                part_kind = getattr(part, "kind", None)
                if part_kind == "command":
                    cmd_str = _node_to_original_string(part, command)
                    if cmd_str.strip():
                        sub_commands.append(cmd_str.strip())
                elif part_kind == "pipe":
                    continue  # 跳过管道符本身
                else:
                    cmd_str = _node_to_original_string(part, command)
                    if cmd_str.strip():
                        sub_commands.append(cmd_str.strip())

        else:
            # 其他类型节点，尝试转换为字符串
            cmd_str = _node_to_original_string(node, command)
            if cmd_str.strip():
                sub_commands.append(cmd_str.strip())

    return [c for c in sub_commands if c.strip()]


def _nodes_to_command_string(nodes: list[Any], original_command: str) -> str:
    """将多个 AST 节点组合为命令字符串，使用 pos 从原始命令切片。"""
    if not nodes:
        return ""
    # 取所有节点的最小 start 和最大 end，从原始命令中切片
    starts: list[int] = []
    ends: list[int] = []
    for node in nodes:
        if hasattr(node, "pos"):
            starts.append(node.pos[0])
            ends.append(node.pos[1])
    if starts and ends:
        return original_command[min(starts) : max(ends)]
    # fallback：逐个节点切片再拼接
    parts_strs: list[str] = []
    for node in nodes:
        parts_strs.append(_node_to_original_string(node, original_command))
    return " ".join(parts_strs)


def _node_to_original_string(node: Any, original_command: str) -> str:
    """从 AST 节点还原原始命令字符串。

    使用节点的 pos 属性从原始命令字符串中切片，保留引号等原始语法。
    """
    if hasattr(node, "pos"):
        start, end = node.pos
        return original_command[start:end]

    # fallback for nodes without pos
    kind = getattr(node, "kind", None)

    if kind == "pipe":
        return "|"

    if hasattr(node, "word"):
        return node.word

    if hasattr(node, "parts") and node.parts:
        return "".join(_part_to_string(p) for p in node.parts)

    return str(node)


# ========== bash -c 检测与提取 ==========


def _is_bash_c_form(cmd_name: str, args: list[str]) -> bool:
    """判断是否为 bash -c "command" 形式。

    Args:
        cmd_name: 规范化后的命令名。
        args: 参数列表。

    Returns:
        是否为 bash -c 形式。
    """
    if cmd_name not in ("bash", "sh", "zsh"):
        return False
    # 检查是否有 -c 参数
    return "-c" in args


def _extract_bash_c_content(cmd_name: str, args: list[str]) -> str | None:
    """从 bash -c 形式的命令中提取要执行的内部命令。

    Args:
        cmd_name: 规范化后的命令名。
        args: 参数列表。

    Returns:
        内部命令字符串，如果未找到则返回 None。
    """
    if not _is_bash_c_form(cmd_name, args):
        return None

    try:
        c_index = args.index("-c")
        if c_index + 1 < len(args):
            return args[c_index + 1]
    except ValueError:
        pass
    return None


# ========== 禁止模式检测 ==========


def _check_rejected_patterns(command: str) -> tuple[bool, str]:
    """检测命令中是否包含禁止的操作符或模式。

    在 bashlex 解析之前进行预检测，捕获需要在原始文本层面
    拒绝的模式（如命令替换、进程替换、后台执行、重定向等）。

    Args:
        command: 原始命令字符串。

    Returns:
        (是否包含禁止模式, 拒绝原因) 元组。
    """
    # 检测命令替换 $(cmd) 或 `cmd`
    if _COMMAND_SUBSTITUTION_PATTERN.search(command):
        return False, "命令中包含命令替换语法（$(...) 或 `...`），不允许执行"

    # 检测进程替换 <(...) 或 >(...)
    if re.search(r"<\(|>\(", command):
        return False, "命令中包含进程替换语法（<() 或 >()），不允许执行"

    # 检测 Here String <<<
    if re.search(r"<<<", command):
        return False, "命令中包含 Here String 语法（<<<），不允许执行"

    # 检测 Here Document <<
    if re.search(r"<<(?!<<)(?!\()", command):
        return False, "命令中包含 Here Document 语法（<<），不允许执行"

    # 检测重定向操作符（>、>>、< 等）
    # 先排除所有重定向到 /dev/null 的安全模式，再检测是否还有其他重定向
    if _REDIRECT_PATTERN.search(command):
        # 将重定向到 /dev/null 的部分临时移除后，再检测是否还存在其他重定向
        sanitized = _REDIRECT_TO_DEV_NULL_PATTERN.sub("", command)
        if _REDIRECT_PATTERN.search(sanitized):
            return False, "命令中包含重定向操作符（>、>>、< 等），不允许使用重定向"

    # 检测 |& 操作符（管道+stderr 重定向）
    if re.search(r"\|\&", command):
        return False, "命令中包含 |& 操作符，不允许执行"

    # 检测后台执行符 &
    # 需要排除 &&（逻辑与）和 |& 的情况
    # 匹配单独的 &，不在 && 或 |& 中
    if re.search(r"(?<![&\|])\&(?!\&)", command):
        return False, "命令中包含后台执行符（&），不允许在后台执行命令"

    # 检测 nohup
    if re.search(r"\bnohup\b", command):
        return False, "命令中包含 nohup，不允许脱离终端执行"

    # 检测 setsid
    if re.search(r"\bsetsid\b", command):
        return False, "命令中包含 setsid，不允许新建会话执行"

    # 检测 disown
    if re.search(r"\bdisown\b", command):
        return False, "命令中包含 disown，不允许脱离终端"

    # 检测 screen / tmux
    if re.search(r"\bscreen\b", command):
        return False, "命令中包含 screen，不允许使用终端复用器"

    if re.search(r"\btmux\b", command):
        return False, "命令中包含 tmux，不允许使用终端复用器"

    # 检测大括号扩展
    if _BRACE_EXPANSION_PATTERN.search(command):
        return False, "命令中包含大括号扩展语法（{}），不允许执行"

    return True, ""


# ========== 脚本路径校验 ==========


def _check_script_path_allowed(
    script_path: str,
    allowed_dirs: list[str],
) -> tuple[bool, str]:
    """检查脚本路径是否在允许的目录内。

    Args:
        script_path: 脚本文件路径。
        allowed_dirs: 允许的目录列表。

    Returns:
        (是否允许, 拒绝原因) 元组。
    """
    if not script_path:
        return False, "未指定脚本路径"

    # 规范化路径
    normalized = os.path.normpath(script_path)

    # 拒绝绝对路径中的遍历
    if ".." in normalized:
        return False, f"脚本路径包含目录遍历: {script_path}"

    # 检查是否在允许的目录内
    for allowed_dir in allowed_dirs:
        allowed_normalized = os.path.normpath(allowed_dir)
        # 路径必须是 allowed_dir 的直接或间接子路径
        if normalized == allowed_normalized:
            return True, ""
        if normalized.startswith(allowed_normalized + os.sep):
            return True, ""

    return False, f"脚本路径不在允许的目录内（允许: {', '.join(allowed_dirs)}）"


def _extract_script_argument(args: list[str]) -> str | None:
    """从参数列表中提取脚本文件路径。

    跳过已知的标志参数，取第一个非标志参数作为脚本路径。

    Args:
        args: 参数列表。

    Returns:
        脚本路径，如果未找到则返回 None。
    """
    skip_next = False
    for i, arg in enumerate(args):
        if skip_next:
            skip_next = False
            continue
        # 跳过 -c 及其参数
        if arg == "-c":
            skip_next = True
            continue
        # 跳过其他可能带值的标志
        if arg in ("-o", "--options", "-O"):
            skip_next = True
            continue
        # 非标志参数，认为是脚本路径
        if not arg.startswith("-"):
            return arg
    return None
