# -*- coding: utf-8 -*-
"""T-15：``build_tool_node`` 挂点与 wrapper 顺序不变式（REQ-15 / REQ-16）。"""

from __future__ import annotations

from unittest.mock import patch

from aidev_agent.core.nodes.tool import ToolNodeSettings, build_tool_node
from aidev_agent.core.nodes.tool import node as node_module
from aidev_agent.core.nodes.tool.approval_wrapper import ItsmApprovalStrategy
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolCallRequest


class RecordingBus:
    def __init__(self, decisions: dict[str, dict] | None = None, *, enabled_points=None):
        self.decisions = decisions or {}
        self.calls: list[tuple[str, dict]] = []
        self._enabled = enabled_points

    def enabled(self, point) -> bool:
        if self._enabled is None:
            return True
        return getattr(point, "value", point) in self._enabled

    def points(self):
        return frozenset(self.decisions)

    def emit(self, point, payload: dict) -> dict:
        self.calls.append((getattr(point, "value", point), payload))
        return self.decisions.get(getattr(point, "value", point), {})


def _capture_chain(*, hooks=None, node_options: ToolNodeSettings | None = None) -> list:
    """构造一次 tool node 并取回同步 wrapper 链（按外层→内层）。"""
    captured: dict[str, list] = {}
    original = node_module._chain_tool_call_wrappers

    def _capture(wrappers):
        captured["wrappers"] = list(wrappers)
        return original(wrappers)

    with patch.object(node_module, "_chain_tool_call_wrappers", side_effect=_capture):
        build_tool_node(tools=[_plain_tool], hooks=hooks, node_options=node_options or ToolNodeSettings())
    return captured["wrappers"]


def _chain_names(**kwargs) -> list[str]:
    return [getattr(wrapper, "__name__", type(wrapper).__name__) for wrapper in _capture_chain(**kwargs)]


def _compose(wrappers: list):
    return node_module._chain_tool_call_wrappers(wrappers)


#: 功能断言用的最小内置链（关掉计时等需要图 runtime 的 wrapper，只保留审批 / 去重）
_SIMPLE_OPTIONS = ToolNodeSettings(use_timer=False)


def _make_request(call_id: str = "call-1") -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "approval_tool", "args": {"q": "x"}, "id": call_id},
        tool=_approval_tool,
        state={},
        runtime=None,
    )


@tool
def _plain_tool(q: str) -> str:
    """plain tool."""
    return q


@tool
def _approval_tool(q: str) -> str:
    """tool requiring ITSM approval."""
    return q


_approval_tool.metadata = {"approval": {"enabled": True}}


# ============================== 顺序不变式 ==============================


def test_chain_identical_when_hooks_is_none():
    baseline = _chain_names()
    assert _chain_names(hooks=None) == baseline
    assert baseline[-1] == "deduplicate_sync"


def test_chain_identical_when_points_disabled():
    baseline = _chain_names()
    bus = RecordingBus({}, enabled_points=set())
    assert _chain_names(hooks=bus) == baseline


def test_tool_pre_is_first_and_tool_post_is_last_inside_deduplicate():
    bus = RecordingBus(
        {"tool_pre": {"decision": "allow"}, "tool_post": {"decision": "accept"}},
        enabled_points={"tool_pre", "tool_post"},
    )
    names = _chain_names(hooks=bus)

    assert names[0] == "hook_pre_wrapper"
    assert names[-1] == "hook_post_wrapper"
    assert names.index("deduplicate_sync") < names.index("hook_post_wrapper")
    # 审批 wrapper 位于 hook_pre 之后（deny 才能早于审批）
    assert names.index("hook_pre_wrapper") < names.index("itsm_approval_sync_wrapper")


def test_only_enabled_point_is_inserted():
    bus = RecordingBus({"tool_post": {"decision": "accept"}}, enabled_points={"tool_post"})
    names = _chain_names(hooks=bus)

    assert "hook_pre_wrapper" not in names
    assert names[-1] == "hook_post_wrapper"


# ============================== 功能断言 ==============================


def test_tool_pre_deny_skips_itsm_approval_and_tool_body():
    bus = RecordingBus({"tool_pre": {"decision": "deny", "reason": "被 hook 拦下"}}, enabled_points={"tool_pre"})
    chain = _compose(_capture_chain(hooks=bus, node_options=_SIMPLE_OPTIONS))
    executed: list[int] = []

    with patch.object(ItsmApprovalStrategy, "interrupt", autospec=True) as approval_interrupt:
        result = chain(_make_request(), lambda request: executed.append(1) or _identity(request))

    assert executed == [], "deny 必须早于工具本体"
    assert approval_interrupt.call_count == 0, "deny 必须早于 ITSM 审批"
    assert result.status == "error"
    assert result.content == "被 hook 拦下"


def test_tool_pre_allow_still_enters_itsm_approval():
    bus = RecordingBus({"tool_pre": {"decision": "allow"}}, enabled_points={"tool_pre"})
    chain = _compose(_capture_chain(hooks=bus, node_options=_SIMPLE_OPTIONS))
    executed: list[int] = []

    with patch.object(ItsmApprovalStrategy, "interrupt", autospec=True, return_value=(None, None)) as approval_interrupt:
        chain(_make_request(), lambda request: executed.append(1) or _identity(request))

    assert approval_interrupt.call_count == 1, "allow 只是 hook 不拦，不得绕开 ITSM 审批"
    assert executed == [1]


def test_tool_post_sees_raw_result_after_tool_body():
    bus = RecordingBus({"tool_post": {"decision": "content", "content": "改写"}}, enabled_points={"tool_post"})
    chain = _compose(_capture_chain(hooks=bus, node_options=_SIMPLE_OPTIONS))
    request = ToolCallRequest(
        tool_call={"name": "plain", "args": {"q": "x"}, "id": "call-9"}, tool=_plain_tool, state={}, runtime=None
    )

    result = chain(request, lambda req: ToolMessage(content="raw", tool_call_id="call-9", name="plain"))

    assert result.content == "改写"
    assert bus.calls[0][1]["tool_response"] == "raw"


def _identity(request: ToolCallRequest) -> ToolMessage:
    return ToolMessage(
        content="ok",
        tool_call_id=request.tool_call["id"],
        name=request.tool_call["name"],
    )
