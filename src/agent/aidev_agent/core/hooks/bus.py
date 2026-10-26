# -*- coding: utf-8 -*-
"""hook 点协议：点位枚举、总线协议与裁决取值（REQ-13 / REQ-15 / REQ-16）。

core 层只定义**协议**与**裁决解释**，不依赖 headless，也不解析 ``hooks.json``、
不执行 hook 命令、不实现 matcher —— 判定执行方在宿主（REQ-13「执行归属」）。
本仓对 hook 的职责只有两件：上报「哪个点发生了 + 载荷」，接收「裁决」。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable


class HookPoint(str, Enum):
    """三个 hook 点（值即协议里的 ``point`` 字段）。"""

    SESSION_START = "session_start"
    TOOL_PRE = "tool_pre"
    TOOL_POST = "tool_post"


#: 全部点位取值（配置缺省时三点全开）
ALL_HOOK_POINTS: frozenset[str] = frozenset(point.value for point in HookPoint)

#: ``tool_pre`` 裁决取值
TOOL_PRE_ALLOW = "allow"
TOOL_PRE_DENY = "deny"
TOOL_PRE_ASK = "ask"

#: ``tool_post`` 裁决取值
TOOL_POST_ACCEPT = "accept"
TOOL_POST_BLOCK = "block"
TOOL_POST_CONTENT = "content"
TOOL_POST_CONTEXT = "context"

TOOL_PRE_DECISIONS: frozenset[str] = frozenset({TOOL_PRE_ALLOW, TOOL_PRE_DENY, TOOL_PRE_ASK})
TOOL_POST_DECISIONS: frozenset[str] = frozenset({TOOL_POST_ACCEPT, TOOL_POST_BLOCK, TOOL_POST_CONTENT, TOOL_POST_CONTEXT})

#: 该点最宽松裁决（fail-open 与未知裁决都按它处理，REQ-18）
PERMISSIVE_DECISION: dict[str, str] = {
    HookPoint.TOOL_PRE.value: TOOL_PRE_ALLOW,
    HookPoint.TOOL_POST.value: TOOL_POST_ACCEPT,
}

#: hook「转人工」中断的 reason。沿用仓内 ``aidev:`` 命名空间，只扩展取值、不新增机制。
HOOK_ASK_REASON = "aidev:hook_ask"

#: ``session_start`` 的 context 字段
SESSION_START_CONTEXT_KEY = "context"

#: 降级原因（写进 ``hook_degraded`` 事件的 ``value.reason``，REQ-18）
DEGRADE_TIMEOUT = "timeout"
DEGRADE_HOST_ERROR = "host_error"
DEGRADE_INVALID_SHAPE = "invalid_shape"
DEGRADE_UNKNOWN_DECISION = "unknown_decision"

#: ``hook_degraded`` 事件名（复用既有 ``CUSTOM`` 事件，不新增 EventType）
HOOK_DEGRADED_EVENT_NAME = "hook_degraded"


@runtime_checkable
class HookBusProtocol(Protocol):
    """hook 总线协议（鸭子类型，不做 ``isinstance`` 校验）。

    实现方（如 headless 的 ``HeadlessHookBus``）负责把 ``emit`` 变成一次宿主往返；
    core 层只按本协议调用。三点之外的实现细节（超时、取消、降级事件）不属本协议
    约束，由实现方自行决定。
    """

    def enabled(self, point: HookPoint | str) -> bool:
        """该点是否启用。"""
        ...

    def points(self) -> frozenset[str]:
        """已启用的点位集合。"""
        ...

    def emit(self, point: HookPoint | str, payload: dict[str, Any]) -> dict[str, Any]:
        """上报一个点位并返回裁决；无裁决 / 不可用时返回空 dict（fail-open）。"""
        ...


def point_value(point: HookPoint | str) -> str:
    """把点位入参统一成字符串取值。"""
    return point.value if isinstance(point, HookPoint) else str(point)


def bus_enabled(bus: Any, point: HookPoint | str) -> bool:
    """安全询问总线某点位是否启用；总线为 ``None`` 或未实现 ``enabled`` 时视为关闭。"""
    if bus is None:
        return False
    enabled = getattr(bus, "enabled", None)
    if not callable(enabled):
        return False
    return bool(enabled(point))


def report_degraded(bus: Any, point: HookPoint | str, reason: str) -> None:
    """把「本点降级」上报给总线（可选能力，未实现则忽略）。"""
    notify = getattr(bus, "notify_degraded", None)
    if callable(notify):
        notify(point_value(point), reason)


def interpret_tool_pre(result: Any) -> tuple[str, str | None]:
    """解释 ``tool_pre`` 裁决。

    :return: ``(decision, degrade_reason)``；无法判定时为最宽松的 ``allow``，
        并在 ``degrade_reason`` 给出降级原因（未知取值 → ``unknown_decision``）。
    """
    if not isinstance(result, dict):
        return PERMISSIVE_DECISION[HookPoint.TOOL_PRE.value], None if result is None else DEGRADE_INVALID_SHAPE
    decision = result.get("decision")
    if decision is None:
        return TOOL_PRE_ALLOW, None
    if not isinstance(decision, str):
        return TOOL_PRE_ALLOW, DEGRADE_UNKNOWN_DECISION
    normalized = decision.strip().lower()
    if normalized not in TOOL_PRE_DECISIONS:
        return TOOL_PRE_ALLOW, DEGRADE_UNKNOWN_DECISION
    return normalized, None


def interpret_tool_post(result: Any) -> tuple[str, str | None]:
    """解释 ``tool_post`` 裁决（``decision`` 缺省时可由 ``content`` / ``context`` 推断）。"""
    if not isinstance(result, dict):
        return PERMISSIVE_DECISION[HookPoint.TOOL_POST.value], None if result is None else DEGRADE_INVALID_SHAPE
    decision = result.get("decision")
    if decision is None:
        if result.get("content") is not None:
            return TOOL_POST_CONTENT, None
        if result.get("context"):
            return TOOL_POST_CONTEXT, None
        return TOOL_POST_ACCEPT, None
    if not isinstance(decision, str):
        return TOOL_POST_ACCEPT, DEGRADE_UNKNOWN_DECISION
    normalized = decision.strip().lower()
    if normalized not in TOOL_POST_DECISIONS:
        return TOOL_POST_ACCEPT, DEGRADE_UNKNOWN_DECISION
    return normalized, None


def decode_ask_answer(answer: Any) -> str:
    """把 ``interrupt()`` 的恢复值解释为 ``allow`` / ``deny``。

    缺裁决或取值未知时按 ``deny`` 处理（宁可不执行，也不误放行，见方案 D-14）。
    """
    if isinstance(answer, dict):
        answer = answer.get("decision")
    if isinstance(answer, str) and answer.strip().lower() == TOOL_PRE_ALLOW:
        return TOOL_PRE_ALLOW
    return TOOL_PRE_DENY


__all__ = [
    "ALL_HOOK_POINTS",
    "DEGRADE_HOST_ERROR",
    "DEGRADE_INVALID_SHAPE",
    "DEGRADE_TIMEOUT",
    "DEGRADE_UNKNOWN_DECISION",
    "HOOK_ASK_REASON",
    "HOOK_DEGRADED_EVENT_NAME",
    "HookBusProtocol",
    "HookPoint",
    "PERMISSIVE_DECISION",
    "SESSION_START_CONTEXT_KEY",
    "TOOL_POST_ACCEPT",
    "TOOL_POST_BLOCK",
    "TOOL_POST_CONTENT",
    "TOOL_POST_CONTEXT",
    "TOOL_PRE_ALLOW",
    "TOOL_PRE_ASK",
    "TOOL_PRE_DENY",
    "bus_enabled",
    "decode_ask_answer",
    "interpret_tool_post",
    "interpret_tool_pre",
    "point_value",
    "report_degraded",
]
