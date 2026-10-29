# -*- coding: utf-8 -*-
"""``tool_pre`` / ``tool_post`` 的工具 wrapper 工厂（REQ-15 / REQ-16）。

四个工厂（同步 / 异步 × pre / post）都只依赖 ``ToolCallRequest`` 与总线协议：

- ``tool_pre``：``deny`` 早退返回 error 结果的 ``ToolMessage``（不调 ``execute``，
  因此不进审批与脱敏）；``ask`` 调 ``interrupt()`` 转人工（复用仓内既有中断原语，
  不新造审批通道）；``allow`` 原样继续既有链路。
- ``tool_post``：``block`` / ``content`` / ``context`` 只改同一条 ``ToolMessage``，
  ``tool_call_id`` / ``name`` / ``additional_kwargs`` 保持不变。

与 ITSM 权限审批的关系（REQ-13 边界）：``allow`` 只是「hook 层不拦」，之后仍完整走
审批 wrapper；``deny`` 只是更严格的早退；``ask`` 是**额外一次**人工裁决，不等于审批
通过。本模块不读取、不短路审批链路。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command, interrupt

from .bus import (
    HOOK_ASK_REASON,
    TOOL_POST_BLOCK,
    TOOL_POST_CONTENT,
    TOOL_POST_CONTEXT,
    TOOL_PRE_ALLOW,
    TOOL_PRE_ASK,
    TOOL_PRE_DENY,
    HookPoint,
    decode_ask_answer,
    interpret_tool_post,
    interpret_tool_pre,
    report_degraded,
)

#: ``tool_post`` 追加 context 时的分隔块标记（模型可见，标注来源为 hook）
HOOK_CONTEXT_SEPARATOR = "\n\n[hook context]\n"

ToolResult = ToolMessage | Command


# ============================== 载荷 ==============================


def _tool_call(request: ToolCallRequest) -> dict[str, Any]:
    return getattr(request, "tool_call", None) or {}


def build_tool_pre_payload(request: ToolCallRequest) -> dict[str, Any]:
    """``tool_pre`` 载荷：只含三个业务字段，不带工具对象与图状态（REQ-15）。"""
    tool_call = _tool_call(request)
    return {
        "tool_name": tool_call.get("name") or "",
        "tool_input": tool_call.get("args") or {},
        "tool_use_id": tool_call.get("id") or "",
    }


def _tool_response_text(result: Any) -> str:
    """把工具返回值压成可上报的文本（保持原值语义，不做截断）。"""
    content = getattr(result, "content", None)
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - 兜底不可序列化内容
        return str(content)


def build_tool_post_payload(request: ToolCallRequest, result: Any) -> dict[str, Any]:
    """``tool_post`` 载荷：``tool_pre`` 三字段 + ``tool_response``（REQ-16）。"""
    payload = build_tool_pre_payload(request)
    payload["tool_response"] = _tool_response_text(result)
    return payload


# ============================== 裁决落地 ==============================


def _tool_message(request: ToolCallRequest, content: Any, *, status: str | None = None, base: ToolMessage | None = None):
    """构造/改写 ``ToolMessage``：``tool_call_id`` / ``name`` / ``additional_kwargs`` 恒定不变。"""
    tool_call = _tool_call(request)
    kwargs: dict[str, Any] = {
        "content": content,
        "tool_call_id": tool_call.get("id") or "",
        "name": tool_call.get("name") or "",
        "additional_kwargs": dict(base.additional_kwargs if base is not None else {}),
    }
    if status is not None:
        kwargs["status"] = status
    return ToolMessage(**kwargs)


def _denied(request: ToolCallRequest, reason: str, *, base: ToolMessage | None = None) -> ToolMessage:
    """失败结果的 ``ToolMessage``（``deny`` / ``block`` / ``ask=deny`` 共用）。"""
    return _tool_message(request, reason, status="error", base=base)


def _ask_value(payload: dict[str, Any], reason: Any) -> dict[str, Any]:
    """``ask`` 的中断值：形状与审批中断同构（``reason`` / ``message`` / ``toolCallId``）。"""
    return {
        "reason": HOOK_ASK_REASON,
        "message": reason or f"工具 {payload.get('tool_name') or ''} 需要人工确认",
        "toolCallId": payload.get("tool_use_id") or "",
    }


def _apply_post_decision(request: ToolCallRequest, result: ToolMessage, verdict: dict[str, Any], decision: str):
    """按裁决改写同一条 ``ToolMessage``。"""
    if decision == TOOL_POST_BLOCK:
        return _denied(request, str(verdict.get("reason") or "hook 已阻断该工具结果"), base=result)
    if decision == TOOL_POST_CONTENT:
        content = verdict.get("content", result.content)
        context = verdict.get("context")
        # content 与 context 同时给出时先替换再追加（裁剪顺序固定）
        if context:
            content = f"{content}{HOOK_CONTEXT_SEPARATOR}{context}"
        return _tool_message(request, content, base=result)
    if decision == TOOL_POST_CONTEXT:
        context = verdict.get("context")
        if not context:
            return result
        return _tool_message(request, f"{result.content}{HOOK_CONTEXT_SEPARATOR}{context}", base=result)
    return result


# ============================== 裁决获取 ==============================


def _cached_ask(bus: Any, tool_use_id: str) -> str | None:
    """重放轮读取 memo：同一 ``tool_use_id`` 只上报一次 ``tool_pre``（方案 D1）。"""
    getter = getattr(bus, "ask_decision", None)
    if not callable(getter) or not tool_use_id:
        return None
    memo = getter(tool_use_id)
    return str(memo) if memo else None


def _remember_ask(bus: Any, tool_use_id: str) -> None:
    remember = getattr(bus, "remember_ask", None)
    if callable(remember) and tool_use_id:
        remember(tool_use_id)


def _pre_verdict(bus: Any, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """返回 ``(verdict, decision)``；重放命中 memo 时 ``verdict=None, decision=ask``。"""
    if _cached_ask(bus, payload.get("tool_use_id") or ""):
        return None, TOOL_PRE_ASK
    verdict = bus.emit(HookPoint.TOOL_PRE, payload)
    decision, degrade_reason = interpret_tool_pre(verdict)
    if degrade_reason:
        report_degraded(bus, HookPoint.TOOL_PRE, degrade_reason)
    if decision == TOOL_PRE_ASK:
        _remember_ask(bus, payload.get("tool_use_id") or "")
    return verdict if isinstance(verdict, dict) else {}, decision


async def _pre_verdict_async(bus: Any, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """异步版 ``_pre_verdict``：阻塞等待下放线程池，不占事件循环（方案 D-10）。"""
    if _cached_ask(bus, payload.get("tool_use_id") or ""):
        return None, TOOL_PRE_ASK
    loop = asyncio.get_running_loop()
    verdict = await loop.run_in_executor(None, bus.emit, HookPoint.TOOL_PRE, payload)
    decision, degrade_reason = interpret_tool_pre(verdict)
    if degrade_reason:
        report_degraded(bus, HookPoint.TOOL_PRE, degrade_reason)
    if decision == TOOL_PRE_ASK:
        _remember_ask(bus, payload.get("tool_use_id") or "")
    return verdict if isinstance(verdict, dict) else {}, decision


def _post_verdict(bus: Any, request: ToolCallRequest, result: ToolMessage) -> tuple[dict[str, Any], str]:
    verdict = bus.emit(HookPoint.TOOL_POST, build_tool_post_payload(request, result))
    decision, degrade_reason = interpret_tool_post(verdict)
    if degrade_reason:
        report_degraded(bus, HookPoint.TOOL_POST, degrade_reason)
    return verdict if isinstance(verdict, dict) else {}, decision


async def _post_verdict_async(bus: Any, request: ToolCallRequest, result: ToolMessage) -> tuple[dict[str, Any], str]:
    loop = asyncio.get_running_loop()
    verdict = await loop.run_in_executor(None, bus.emit, HookPoint.TOOL_POST, build_tool_post_payload(request, result))
    decision, degrade_reason = interpret_tool_post(verdict)
    if degrade_reason:
        report_degraded(bus, HookPoint.TOOL_POST, degrade_reason)
    return verdict if isinstance(verdict, dict) else {}, decision


# ============================== 同步 wrapper ==============================


def build_hook_pre_sync_wrapper(
    bus: Any,
) -> Callable[[ToolCallRequest, Callable[[ToolCallRequest], ToolResult]], ToolResult]:
    """``tool_pre`` 同步 wrapper（插在 wrapper 链首：先于净化 / 脱敏 / ITSM 审批）。"""

    def hook_pre_wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], ToolResult],
    ) -> ToolResult:
        payload = build_tool_pre_payload(request)
        verdict, decision = _pre_verdict(bus, payload)
        if decision == TOOL_PRE_DENY:
            return _denied(request, str((verdict or {}).get("reason") or "hook 已拒绝该工具调用"))
        if decision == TOOL_PRE_ASK:
            answer = interrupt(_ask_value(payload, (verdict or {}).get("reason")))
            if decode_ask_answer(answer) == TOOL_PRE_ALLOW:
                return execute(request)
            return _denied(request, "hook 人工裁决未放行，工具未执行")
        return execute(request)

    return hook_pre_wrapper


def build_hook_post_sync_wrapper(
    bus: Any,
) -> Callable[[ToolCallRequest, Callable[[ToolCallRequest], ToolResult]], ToolResult]:
    """``tool_post`` 同步 wrapper（插在链尾：紧贴工具本体、在 ``deduplicate`` 之内）。"""

    def hook_post_wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], ToolResult],
    ) -> ToolResult:
        result = execute(request)
        if not isinstance(result, ToolMessage):
            return result
        verdict, decision = _post_verdict(bus, request, result)
        return _apply_post_decision(request, result, verdict, decision)

    return hook_post_wrapper


# ============================== 异步 wrapper ==============================


def build_hook_pre_async_wrapper(
    bus: Any,
) -> Callable[[ToolCallRequest, Callable[[ToolCallRequest], Awaitable[ToolResult]]], Awaitable[ToolResult]]:
    """``tool_pre`` 异步 wrapper。"""

    async def hook_pre_awrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolResult]],
    ) -> ToolResult:
        payload = build_tool_pre_payload(request)
        verdict, decision = await _pre_verdict_async(bus, payload)
        if decision == TOOL_PRE_DENY:
            return _denied(request, str((verdict or {}).get("reason") or "hook 已拒绝该工具调用"))
        if decision == TOOL_PRE_ASK:
            answer = interrupt(_ask_value(payload, (verdict or {}).get("reason")))
            if decode_ask_answer(answer) == TOOL_PRE_ALLOW:
                return await execute(request)
            return _denied(request, "hook 人工裁决未放行，工具未执行")
        return await execute(request)

    return hook_pre_awrapper


def build_hook_post_async_wrapper(
    bus: Any,
) -> Callable[[ToolCallRequest, Callable[[ToolCallRequest], Awaitable[ToolResult]]], Awaitable[ToolResult]]:
    """``tool_post`` 异步 wrapper。"""

    async def hook_post_awrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolResult]],
    ) -> ToolResult:
        result = await execute(request)
        if not isinstance(result, ToolMessage):
            return result
        verdict, decision = await _post_verdict_async(bus, request, result)
        return _apply_post_decision(request, result, verdict, decision)

    return hook_post_awrapper


__all__ = [
    "HOOK_CONTEXT_SEPARATOR",
    "build_hook_post_async_wrapper",
    "build_hook_post_sync_wrapper",
    "build_hook_pre_async_wrapper",
    "build_hook_pre_sync_wrapper",
    "build_tool_post_payload",
    "build_tool_pre_payload",
]
