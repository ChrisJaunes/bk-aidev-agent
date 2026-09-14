# -*- coding: utf-8 -*-
"""
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

工具安全包装器集合。

本模块把三个工具级安全包装器收敛到单一文件，共享「前置门禁 → 短路或放行」的
同步/异步样板，减少三份近乎重复的实现：

1. 网络白名单（前置短路）
   在工具执行前扫描 ``request.tool_call["args"]`` 中的 URL / 域名，命中未授权域名
   即短路返回错误 ToolMessage，阻止数据外泄到未授权目标。
   仅当 allow / block 列表至少一项非空时才生效；两者皆空时 fail-open
   （私网 / 回环地址仍会阻断），不影响既有工具行为。

2. Reward Hacking 防护（前置短路）
   在工具执行前识别危险意图：同一危险目标（域名 / IP / 路径）在会话内被重复尝试
   （换工具 / 换措辞），即判定为 reward hacking，短路返回升级阻断消息并告警。
   首次危险意图不在此拦截（由命令白名单 / 文件敏感路径 / 网络白名单等硬约束
   拦截），本包装器只负责识别重复尝试并升级处置。

3. 结果脱敏与不可信包裹（后置改写）
   对工具返回的 ToolMessage 按 ``MODEL_OUTPUT`` purpose 脱敏所有凭据（typed sentinel，
   不保留 secret body），并对不可信工具
   （web / mcp 等外部数据）做原型污染键清理 + 注入痕迹扫描 + 不可信内容包裹，
   引导模型把外部数据当作数据处理而非指令执行。

所有 ``enabled`` / ``allow_domains`` / ``block_domains`` 参数均须由调用方显式传入
（``core/nodes/tool/node.py`` 从 ``ToolNodeSettings`` 拆解后注入）；结果脱敏所需的
``settings``（``SecuritySettings``，含已知敏感值）同样由调用方注入。
本模块不再读取环境变量 —— 安全配置的唯一入口是 ``AgentConfig.security_settings``。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import AsyncToolCallWrapper, ToolCallRequest, ToolCallWrapper
from langgraph.types import Command

from aidev_agent.packages.security.network_allowlist import evaluate_hosts, extract_hosts
from aidev_agent.packages.security.redaction import (
    RedactionPurpose,
    redact_payload,
    redact_text,
)
from aidev_agent.packages.security.reward_hacking import (
    _ledger,
    detect_dangerous_intent,
    intent_fingerprint,
    thread_key,
)
from aidev_agent.packages.security.threat_patterns import (
    is_untrusted_tool,
    sanitize_pollution_keys,
    scan_for_threats,
    wrap_untrusted_content,
)
from aidev_agent.pydantic_models import SecuritySettings

logger = logging.getLogger(__name__)

_NETWORK_BLOCK_MESSAGE = "网络/域名白名单拦截：目标域名不在允许列表内，已阻断本次调用。"
_REWARD_HACKING_ESCALATE_MESSAGE = (
    "Reward Hacking 防护：检测到对同一危险目标（此前已被拦截）的重复尝试，"
    "已升级阻断。请停止绕过安全边界的尝试，转而向用户说明无法执行。"
)


# ============================================================================
# 共享样板：前置门禁 + 同步/异步包装器构造
# ============================================================================


def _build_gated_wrapper(
    pre_check: Callable[[ToolCallRequest], ToolMessage | None],
) -> tuple[ToolCallWrapper, AsyncToolCallWrapper]:
    """把「前置门禁 → 短路或放行」的同步/异步样板收敛为一次定义。

    ``pre_check`` 返回非 ``None`` 的 ToolMessage 时短路返回（不执行原工具），
    返回 ``None`` 时放行到 ``execute``。
    """

    def sync_wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        blocked = pre_check(request)
        if blocked is not None:
            return blocked
        return execute(request)

    async def async_wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        blocked = pre_check(request)
        if blocked is not None:
            return blocked
        return await execute(request)

    return sync_wrapper, async_wrapper


def _tool_name(request: ToolCallRequest) -> str:
    """从 request.tool_call 解析工具名（dict 以外视为空）。"""
    return request.tool_call.get("name", "") if isinstance(request.tool_call, dict) else ""


def _tool_call_id(request: ToolCallRequest) -> str:
    """从 request.tool_call 解析调用 id（dict 以外视为空）。"""
    return request.tool_call.get("id", "") if isinstance(request.tool_call, dict) else ""


# ============================================================================
# 1. 网络/域名白名单（前置短路）
# ============================================================================


def _flatten_strings(obj: object) -> list[str]:
    """递归展开 args 中所有字符串值（dict / list / tuple / set / 标量）。"""
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        out: list[str] = []
        for v in obj.values():
            out.extend(_flatten_strings(v))
        return out
    if isinstance(obj, (list, tuple, set)):
        out = []
        for v in obj:
            out.extend(_flatten_strings(v))
        return out
    return [str(obj)] if obj is not None else []


def _check_network_request(
    request: ToolCallRequest,
    enabled: bool,
    allow: list[str],
    block: list[str],
) -> ToolMessage | None:
    if not enabled:
        return None
    if not allow and not block:
        return None  # 未配置任何策略，fail-open

    args = request.tool_call.get("args", {}) if isinstance(request.tool_call, dict) else {}
    hosts: list[str] = []
    for s in _flatten_strings(args):
        hosts.extend(extract_hosts(s))

    allowed, blocked = evaluate_hosts(hosts, allow, block)
    if allowed:
        return None
    logger.warning(
        "[NetworkAllowlist] tool=%s blocked_hosts=%s",
        _tool_name(request),
        blocked,
    )
    return ToolMessage(
        content=_NETWORK_BLOCK_MESSAGE + f" 拦截域名: {', '.join(blocked)}",
        tool_call_id=_tool_call_id(request),
        name=_tool_name(request),
        status="error",
    )


def build_network_allowlist_sync_wrapper(
    enabled: bool,
    allow_domains: list[str],
    block_domains: list[str],
) -> ToolCallWrapper:
    """构建同步网络/域名白名单包装器。

    三个参数均由调用方显式传入（graph 装配层从 ``SecuritySettings`` 拆解后经
    ``ToolNodeSettings`` 注入），本函数不再回落到环境变量。
    """
    return _build_gated_wrapper(lambda request: _check_network_request(request, enabled, allow_domains, block_domains))[
        0
    ]


def build_network_allowlist_async_wrapper(
    enabled: bool,
    allow_domains: list[str],
    block_domains: list[str],
) -> AsyncToolCallWrapper:
    """构建异步网络/域名白名单包装器。参数语义同同步版本。"""
    return _build_gated_wrapper(lambda request: _check_network_request(request, enabled, allow_domains, block_domains))[
        1
    ]


# ============================================================================
# 2. Reward Hacking 防护（前置短路）
# ============================================================================


def _check_reward_hacking_request(
    request: ToolCallRequest,
    enabled: bool,
    allow: list[str] | None,
    block: list[str] | None,
) -> ToolMessage | None:
    if not enabled:
        return None
    name = _tool_name(request)
    args = request.tool_call.get("args", {}) if isinstance(request.tool_call, dict) else {}
    findings = detect_dangerous_intent(name, args, allow=allow, block=block)
    if not findings:
        return None

    fp = intent_fingerprint(name, args, findings)
    key = thread_key(getattr(request, "runtime", None))
    count = _ledger.record(key, fp, name)
    if count >= 2:
        logger.warning(
            "[RewardHacking] thread=%s tool=%s attempt=%d findings=%s",
            key,
            name,
            count,
            findings,
        )
        return ToolMessage(
            content=_REWARD_HACKING_ESCALATE_MESSAGE,
            tool_call_id=_tool_call_id(request),
            name=name,
            status="error",
        )
    # 首次危险意图：记录但不拦截，交由硬约束处理
    logger.info("[RewardHacking] first dangerous intent recorded: tool=%s findings=%s", name, findings)
    return None


def build_reward_hacking_sync_wrapper(
    enabled: bool,
    allow_domains: list[str] | None,
    block_domains: list[str] | None,
) -> ToolCallWrapper:
    """构建同步 Reward Hacking 防护包装器。

    参数由调用方显式传入（装配层从 ``SecuritySettings`` 拆解后注入），
    本函数不再回落到环境变量。
    """
    return _build_gated_wrapper(
        lambda request: _check_reward_hacking_request(request, enabled, allow_domains, block_domains)
    )[0]


def build_reward_hacking_async_wrapper(
    enabled: bool,
    allow_domains: list[str] | None,
    block_domains: list[str] | None,
) -> AsyncToolCallWrapper:
    """构建异步 Reward Hacking 防护包装器。参数语义同同步版本。"""
    return _build_gated_wrapper(
        lambda request: _check_reward_hacking_request(request, enabled, allow_domains, block_domains)
    )[1]


# ============================================================================
# 3. 结果脱敏与不可信包裹（后置改写）
# ============================================================================


def _tool_name_from_message(request: ToolCallRequest, msg: ToolMessage) -> str:
    """从 ToolMessage / request 解析工具名（优先 msg.name）。

    与 :func:`_tool_name` 语义不同：此处优先取已执行结果的 ``msg.name``，
    仅在缺失时回退到 ``request.tool_call``，故独立命名避免遮蔽。
    """
    name = getattr(msg, "name", "") or ""
    if not name:
        name = request.tool_call.get("name", "") if isinstance(request.tool_call, dict) else ""
    return name


def _tool_metadata(request: ToolCallRequest) -> dict:
    """从 request.tool 读取工具元数据（MCP 工具的 mcp_name 在此）。"""
    tool = getattr(request, "tool", None)
    metadata = getattr(tool, "metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def _redact_message_content(msg: ToolMessage, settings: SecuritySettings | None = None) -> None:
    """对 ToolMessage 内容执行脱敏（不落明文到模型上下文）。

    工具结果 → 模型上下文，使用 ``MODEL_OUTPUT`` purpose：掩码走 typed sentinel，
    不保留 secret body（避免模型把 ``ghp_ab...xyz`` 误认为可用 token 并写回配置）。

    已知敏感值随 ``settings`` 下发（``SecuritySettings.known_sensitive_values``）——
    str 与结构化两种形态都透传，避免 dict/list 形态出现覆盖缺口。
    """
    content = msg.content
    if isinstance(content, str):
        msg.content = redact_text(content, purpose=RedactionPurpose.MODEL_OUTPUT, settings=settings)
    elif isinstance(content, (list, tuple, dict)):
        # 结构化 / 多模态块统一走递归脱敏
        msg.content = redact_payload(content, purpose=RedactionPurpose.MODEL_OUTPUT, settings=settings)


def _guard_tool_message(
    request: ToolCallRequest, msg: ToolMessage | Command, settings: SecuritySettings | None = None
) -> ToolMessage | Command:
    """按 ``MODEL_OUTPUT`` purpose 脱敏 + 不可信包裹。

    非 ToolMessage（如 Command）原样透传。
    """
    if not isinstance(msg, ToolMessage):
        return msg

    # 1. 按 MODEL_OUTPUT purpose 脱敏：所有工具结果（含 trusted）都可能携带凭据，统一掩码
    _redact_message_content(msg, settings)

    # 2. 不可信工具结果净化：web / mcp 等外部数据，引导模型当作数据处理
    tool_name = _tool_name_from_message(request, msg)
    tool_metadata = _tool_metadata(request)
    if is_untrusted_tool(tool_name, metadata=tool_metadata):
        # 2a. 原型污染键清理（MCP 返回 JSON 净化，阻断 __proto__/constructor/prototype）
        msg.content = sanitize_pollution_keys(msg.content)
        # 2b. 防御性扫描（scope=all）：命中高置信注入痕迹仅告警，包裹本身是第一道防线
        findings = scan_for_threats(str(msg.content), scope="all")
        if findings:
            logger.warning(
                "[SecurityGuard] untrusted tool=%s returned %d suspicious pattern(s): %s",
                tool_name,
                len(findings),
                [f.pattern_name for f in findings],
            )
        msg.content = wrap_untrusted_content(msg.content, source=tool_name)

    return msg


def build_security_guard_sync_wrapper(settings: SecuritySettings | None = None) -> ToolCallWrapper:
    """构建同步安全防护包装器（脱敏 + 不可信包裹）。

    这是**后置**改写：先执行原工具，再对返回结果净化，故不复用
    :func:`_build_gated_wrapper` 的前置短路样板。

    Args:
        settings: 安全配置（含已知敏感值 / 掩码阈值 / 开关），由调用方从
            ``AgentConfig.security_settings`` 传入。``None`` ⇒ 走 ``SecuritySettings()``
            默认工厂（已知值为空、仍保留模式规则脱敏）。
    """

    def wrapper(
        request: ToolCallRequest, execute: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        msg = execute(request)
        return _guard_tool_message(request, msg, settings)

    return wrapper


def build_security_guard_async_wrapper(settings: SecuritySettings | None = None) -> AsyncToolCallWrapper:
    """构建异步安全防护包装器（脱敏 + 不可信包裹）。参数语义同同步版本。"""

    async def wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        msg = await execute(request)
        return _guard_tool_message(request, msg, settings)

    return wrapper


__all__ = [
    "build_network_allowlist_sync_wrapper",
    "build_network_allowlist_async_wrapper",
    "build_reward_hacking_sync_wrapper",
    "build_reward_hacking_async_wrapper",
    "build_security_guard_sync_wrapper",
    "build_security_guard_async_wrapper",
]
