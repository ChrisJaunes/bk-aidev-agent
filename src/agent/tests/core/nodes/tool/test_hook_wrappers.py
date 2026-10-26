# -*- coding: utf-8 -*-
"""T-14：core hook 协议与四类工具 wrapper（REQ-13 / REQ-15 / REQ-16）。"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from aidev_agent.core.hooks import (
    HOOK_ASK_REASON,
    HookPoint,
    build_hook_post_async_wrapper,
    build_hook_post_sync_wrapper,
    build_hook_pre_async_wrapper,
    build_hook_pre_sync_wrapper,
    bus_enabled,
)
from aidev_agent.core.hooks.wrappers import HOOK_CONTEXT_SEPARATOR
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command


class RecordingBus:
    """记录上报载荷与裁决的 fake 总线。"""

    def __init__(self, decisions: dict[str, dict] | None = None, *, enabled_points=None):
        self.decisions = decisions or {}
        self.calls: list[tuple[str, dict]] = []
        self.degraded: list[tuple[str, str]] = []
        self._enabled = enabled_points
        self._memo: dict[str, str] = {}

    def enabled(self, point) -> bool:
        if self._enabled is None:
            return True
        return getattr(point, "value", point) in self._enabled

    def points(self):
        return frozenset(self.decisions)

    def emit(self, point, payload: dict) -> dict:
        self.calls.append((getattr(point, "value", point), payload))
        return self.decisions.get(getattr(point, "value", point), {})

    def notify_degraded(self, point: str, reason: str) -> None:
        self.degraded.append((point, reason))

    def ask_decision(self, tool_use_id: str):
        return self._memo.get(tool_use_id)

    def remember_ask(self, tool_use_id: str) -> None:
        self._memo[tool_use_id] = "ask"


def _request(name: str = "fake_tool", args: dict | None = None, call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": name, "args": args or {"q": "x"}, "id": call_id},
        tool=None,
        state={},
        runtime=None,
    )


def _ok_message(request: ToolCallRequest, content: str = "tool-result") -> ToolMessage:
    return ToolMessage(
        content=content,
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
        additional_kwargs={"keep": True},
    )


# ============================== tool_pre ==============================


def test_tool_pre_deny_short_circuits_without_execute():
    bus = RecordingBus({"tool_pre": {"decision": "deny", "reason": "危险命令"}})
    executed: list[int] = []

    def execute(request):
        executed.append(1)
        return _ok_message(request)

    result = build_hook_pre_sync_wrapper(bus)(_request(), execute)

    assert executed == [], "deny 不得调用工具本体"
    assert isinstance(result, ToolMessage)
    assert result.status == "error"
    assert result.content == "危险命令"
    assert result.tool_call_id == "call-1"
    assert result.name == "fake_tool"
    assert bus.calls == [("tool_pre", {"tool_name": "fake_tool", "tool_input": {"q": "x"}, "tool_use_id": "call-1"})]


def test_tool_pre_allow_calls_execute_unchanged():
    bus = RecordingBus({"tool_pre": {"decision": "allow"}})

    def execute(request):
        return _ok_message(request)

    result = build_hook_pre_sync_wrapper(bus)(_request(), execute)
    assert result.content == "tool-result"
    assert result.status != "error"


def test_tool_pre_unknown_decision_falls_back_to_allow_and_reports_degraded():
    bus = RecordingBus({"tool_pre": {"decision": "maybe"}})
    executed: list[int] = []

    def execute(request):
        executed.append(1)
        return _ok_message(request)

    build_hook_pre_sync_wrapper(bus)(_request(), execute)

    assert executed == [1]
    assert bus.degraded == [("tool_pre", "unknown_decision")]


def test_tool_pre_ask_raises_interrupt_and_allows_after_resume():
    bus = RecordingBus({"tool_pre": {"decision": "ask", "reason": "需要人工确认"}})
    executed: list[int] = []

    def execute(request):
        executed.append(1)
        return _ok_message(request)

    with patch("aidev_agent.core.hooks.wrappers.interrupt", return_value={"decision": "allow"}) as fake_interrupt:
        result = build_hook_pre_sync_wrapper(bus)(_request(), execute)

    assert executed == [1], "resume 放行后工具必须执行"
    assert result.content == "tool-result"
    value = fake_interrupt.call_args.args[0]
    assert value["reason"] == HOOK_ASK_REASON
    assert value["toolCallId"] == "call-1"
    assert value["message"] == "需要人工确认"


def test_tool_pre_ask_rejected_does_not_execute():
    bus = RecordingBus({"tool_pre": {"decision": "ask"}})
    executed: list[int] = []

    def execute(request):
        executed.append(1)
        return _ok_message(request)

    with patch("aidev_agent.core.hooks.wrappers.interrupt", return_value={"decision": "deny"}):
        result = build_hook_pre_sync_wrapper(bus)(_request(), execute)

    assert executed == []
    assert result.status == "error"


def test_tool_pre_ask_replay_reuses_memo_without_reporting_again():
    bus = RecordingBus({"tool_pre": {"decision": "ask"}})
    with patch("aidev_agent.core.hooks.wrappers.interrupt", return_value={"decision": "allow"}):
        build_hook_pre_sync_wrapper(bus)(_request(), lambda request: _ok_message(request))
        build_hook_pre_sync_wrapper(bus)(_request(), lambda request: _ok_message(request))

    # 同一 tool_use_id 的 tool_pre 只上报一次（方案 D1）
    assert [call[0] for call in bus.calls] == ["tool_pre"]


# ============================== tool_post ==============================


def test_tool_post_payload_and_accept_keeps_result():
    bus = RecordingBus({"tool_post": {"decision": "accept"}})
    request = _request()

    result = build_hook_post_sync_wrapper(bus)(request, lambda req: _ok_message(req, "raw"))

    assert result.content == "raw"
    point, payload = bus.calls[0]
    assert point == "tool_post"
    assert payload == {
        "tool_name": "fake_tool",
        "tool_input": {"q": "x"},
        "tool_use_id": "call-1",
        "tool_response": "raw",
    }


def test_tool_post_block_replaces_result_and_keeps_identity():
    bus = RecordingBus({"tool_post": {"decision": "block", "reason": "含敏感信息"}})
    request = _request()

    result = build_hook_post_sync_wrapper(bus)(request, lambda req: _ok_message(req, "raw"))

    assert result.content == "含敏感信息"
    assert result.status == "error"
    assert result.tool_call_id == "call-1"
    assert result.name == "fake_tool"
    assert result.additional_kwargs == {"keep": True}


def test_tool_post_content_replaces_content_only():
    bus = RecordingBus({"tool_post": {"decision": "content", "content": "改写后"}})
    request = _request()

    result = build_hook_post_sync_wrapper(bus)(request, lambda req: _ok_message(req, "raw"))

    assert result.content == "改写后"
    assert result.tool_call_id == "call-1"
    assert result.name == "fake_tool"
    assert result.additional_kwargs == {"keep": True}


def test_tool_post_context_appends_after_content():
    bus = RecordingBus({"tool_post": {"decision": "content", "content": "改写后", "context": "补充上下文"}})
    request = _request()

    result = build_hook_post_sync_wrapper(bus)(request, lambda req: _ok_message(req, "raw"))

    assert result.content == f"改写后{HOOK_CONTEXT_SEPARATOR}补充上下文"


def test_tool_post_unknown_decision_accepts_and_reports_degraded():
    bus = RecordingBus({"tool_post": {"decision": "nope"}})

    result = build_hook_post_sync_wrapper(bus)(_request(), lambda req: _ok_message(req, "raw"))

    assert result.content == "raw"
    assert bus.degraded == [("tool_post", "unknown_decision")]


def test_tool_post_passes_command_through():
    bus = RecordingBus({"tool_post": {"decision": "block", "reason": "x"}})
    command = Command(update={"messages": []})

    assert build_hook_post_sync_wrapper(bus)(_request(), lambda req: command) is command
    assert bus.calls == []


# ============================== 异步 wrapper ==============================


@pytest.mark.asyncio
async def test_async_wrappers_cover_pre_and_post():
    bus = RecordingBus({"tool_pre": {"decision": "deny", "reason": "no"}, "tool_post": {"decision": "accept"}})

    async def execute(request):
        return _ok_message(request, "raw")

    denied = await build_hook_pre_async_wrapper(bus)(_request(), execute)
    assert denied.status == "error" and denied.content == "no"

    bus.decisions["tool_pre"] = {"decision": "allow"}
    accepted = await build_hook_post_async_wrapper(bus)(_request(), execute)
    assert accepted.content == "raw"


# ============================== 总线协议 ==============================


def test_bus_enabled_handles_missing_and_unknown_bus():
    assert bus_enabled(None, HookPoint.TOOL_PRE) is False
    assert bus_enabled(object(), HookPoint.TOOL_PRE) is False
    assert bus_enabled(RecordingBus(enabled_points={"tool_pre"}), HookPoint.TOOL_PRE) is True
    assert bus_enabled(RecordingBus(enabled_points={"tool_pre"}), HookPoint.TOOL_POST) is False
