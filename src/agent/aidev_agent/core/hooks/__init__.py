# -*- coding: utf-8 -*-
"""core hook 层：点位协议、工具 wrapper 工厂与 ``session_start`` 注入中间件。

本包只定义**协议与挂载件**，不依赖 headless，也不实现宿主侧 hook 引擎
（读 ``hooks.json`` / 跑命令 / matcher / 合并裁决 / 审计均属宿主职责，REQ-13）。
"""

from .bus import (
    ALL_HOOK_POINTS,
    DEGRADE_HOST_ERROR,
    DEGRADE_INVALID_SHAPE,
    DEGRADE_TIMEOUT,
    DEGRADE_UNKNOWN_DECISION,
    HOOK_ASK_REASON,
    HOOK_DEGRADED_EVENT_NAME,
    PERMISSIVE_DECISION,
    SESSION_START_CONTEXT_KEY,
    HookBusProtocol,
    HookPoint,
    bus_enabled,
    decode_ask_answer,
    interpret_tool_post,
    interpret_tool_pre,
    point_value,
    report_degraded,
)
from .middleware import HookContextMiddleware
from .wrappers import (
    build_hook_post_async_wrapper,
    build_hook_post_sync_wrapper,
    build_hook_pre_async_wrapper,
    build_hook_pre_sync_wrapper,
)

__all__ = [
    "ALL_HOOK_POINTS",
    "DEGRADE_HOST_ERROR",
    "DEGRADE_INVALID_SHAPE",
    "DEGRADE_TIMEOUT",
    "DEGRADE_UNKNOWN_DECISION",
    "HOOK_ASK_REASON",
    "HOOK_DEGRADED_EVENT_NAME",
    "PERMISSIVE_DECISION",
    "SESSION_START_CONTEXT_KEY",
    "HookBusProtocol",
    "HookContextMiddleware",
    "HookPoint",
    "bus_enabled",
    "build_hook_post_async_wrapper",
    "build_hook_post_sync_wrapper",
    "build_hook_pre_async_wrapper",
    "build_hook_pre_sync_wrapper",
    "decode_ask_answer",
    "interpret_tool_post",
    "interpret_tool_pre",
    "point_value",
    "report_degraded",
]
