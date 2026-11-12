# -*- coding: utf-8 -*-
"""工具点位测试：tool_pre（deny / replace / ask）与 tool_post（原文、替换、追加、拒绝）。

# AC7 test_tool_pre_deny_blocks_execution
# AC8 test_tool_pre_ask_blocks_when_not_approved / test_tool_pre_ask_executes_when_approved
# AC9 test_tool_post_sees_raw_result_and_flows_through_content_error / test_tool_post_sees_error_on_tool_exception
# AC10 test_tool_post_sees_untruncated_result / test_resolve_real_tool_call_unwraps_proxy_name
"""

from __future__ import annotations

from typing import Annotated, List

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from aidev_agent.core.hook import HookDecision, HookDecisionKind, HookPoint, HookSet
from aidev_agent.core.hook.run_state import get_run_state
from aidev_agent.core.hook.runner import HookRunner
from aidev_agent.core.hook.stages import run_tool_pre_stage
from aidev_agent.core.nodes.tool import ToolNodeSettings, build_tool_node
from aidev_agent.core.nodes.tool.result_limit_wrapper import TOOL_RESULT_TOO_LONG_MESSAGE
from aidev_agent.core.nodes.tool.tool_call_proxy_wrapper import resolve_real_tool_call

from .conftest import make_config

MESSAGES = [HumanMessage(content="hello")]


class ToolState(TypedDict):
    """最简工具节点图状态。"""

    messages: Annotated[List, add_messages]


def _tool_call(tool_call_id: str = "call-1", name: str = "echo", args: dict | None = None) -> dict:
    return {"name": name, "args": args if args is not None else {"query": "origin"}, "id": tool_call_id}


def _state(tool_calls: list[dict]) -> dict:
    return {"messages": [*MESSAGES, AIMessage(content="", tool_calls=tool_calls)]}


def _run_node(node, state: dict, config) -> dict:
    graph = StateGraph(ToolState)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile().invoke(state, config)


def _build_node(tools, hooks: HookSet, **options):
    node_options = ToolNodeSettings(
        use_tool_redaction=False,
        use_tool_untrusted_sanitize=False,
        use_json_repair_on_error=False,
        enable_tool_loop_guard=False,
        **options,
    )
    return build_tool_node(tools=tools, node_options=node_options, hooks=HookRunner(hooks))


def _echo_tool(calls: list[str]):
    @tool
    def echo(query: str) -> str:
        """Echo the query."""
        calls.append(query)
        return f"echo:{query}"

    return echo


def test_tool_pre_deny_blocks_execution() -> None:
    """AC7 tool_pre 的 deny 导致工具不执行，模型收到含拒绝原因的失败结果。"""
    calls: list[str] = []
    hooks = HookSet(
        {HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="not allowed")]}
    )
    node = _build_node([_echo_tool(calls)], hooks)
    config = make_config(hooks)
    state = _state([_tool_call()])
    tools = [_echo_tool(calls)]

    run_tool_pre_stage(state, config, tools)
    result = _run_node(node, state, config)

    message = result["messages"][-1]
    assert calls == []
    assert message.status == "error"
    assert "not allowed" in message.content


def test_tool_pre_replace_changes_arguments() -> None:
    """tool_pre 的 replace 参数生效，工具收到改写后的参数。"""
    calls: list[str] = []
    hooks = HookSet(
        {
            HookPoint.TOOL_PRE: [
                lambda context: HookDecision(kind=HookDecisionKind.REPLACE, payload={"query": "changed"})
            ]
        }
    )
    tools = [_echo_tool(calls)]
    node = _build_node(tools, hooks)
    config = make_config(hooks)
    state = _state([_tool_call()])

    run_tool_pre_stage(state, config, tools)
    _run_node(node, state, config)

    assert calls == ["changed"]


def test_tool_pre_ask_blocks_when_not_approved(monkeypatch) -> None:
    """AC8 tool_pre 的 ask 未获批准时工具不执行。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "aidev_agent.core.nodes.tool.hook_wrapper.interrupt",
        lambda value: [{"payload": {"answers": [{"selectedOptions": ["拒绝执行"]}]}}],
    )
    hooks = HookSet(
        {HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.ASK, reason="need approval")]}
    )
    tools = [_echo_tool(calls)]
    node = _build_node(tools, hooks)
    config = make_config(hooks)
    state = _state([_tool_call()])

    run_tool_pre_stage(state, config, tools)
    result = _run_node(node, state, config)

    assert calls == []
    assert result["messages"][-1].status == "error"


def test_tool_pre_ask_executes_when_approved(monkeypatch) -> None:
    """AC8 tool_pre 的 ask 获批准后继续执行。"""
    calls: list[str] = []
    monkeypatch.setattr(
        "aidev_agent.core.nodes.tool.hook_wrapper.interrupt",
        lambda value: [{"payload": {"answers": [{"selectedOptions": ["允许执行"]}]}}],
    )
    hooks = HookSet(
        {HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.ASK, reason="need approval")]}
    )
    tools = [_echo_tool(calls)]
    node = _build_node(tools, hooks)
    config = make_config(hooks)
    state = _state([_tool_call()])

    run_tool_pre_stage(state, config, tools)
    _run_node(node, state, config)

    assert calls == ["origin"]


def test_tool_pre_triggers_in_model_order_once_per_call() -> None:
    """tool_pre 按模型给出的顺序逐个触发，已完成调用的不再重复触发。"""
    order: list[str] = []

    def handler(context):
        order.append(context.payload.tool_call_id)

    hooks = HookSet({HookPoint.TOOL_PRE: [handler]})
    tools = [_echo_tool([])]
    config = make_config(hooks)
    state = _state([_tool_call("call-1"), _tool_call("call-2"), _tool_call("call-3")])

    run_tool_pre_stage(state, config, tools)
    run_tool_pre_stage(state, config, tools)

    assert order == ["call-1", "call-2", "call-3"]


def test_tool_post_sees_raw_result_and_flows_through_content_error() -> None:
    """AC9/AC10 tool_post 看到未脱敏原文，replace 后继续经 _content_error 重新识别错误。"""
    seen: list[str] = []

    def handler(context):
        seen.append(context.payload.content)
        return HookDecision(kind=HookDecisionKind.REPLACE, payload="[ERROR]replaced")

    hooks = HookSet({HookPoint.TOOL_POST: [handler]})
    tools = [_echo_tool([])]
    node = _build_node(tools, hooks)
    config = make_config(hooks)

    result = _run_node(node, _state([_tool_call()]), config)

    message = result["messages"][-1]
    assert seen == ["echo:origin"]
    assert message.content == "[ERROR]replaced"
    assert message.status == "error"


def test_tool_post_append_keeps_original_content() -> None:
    """tool_post 的 append 不改原结果，追加说明。"""
    hooks = HookSet({HookPoint.TOOL_POST: [lambda context: HookDecision(kind=HookDecisionKind.APPEND, payload="NOTE")]})
    node = _build_node([_echo_tool([])], hooks)

    result = _run_node(node, _state([_tool_call()]), make_config(hooks))

    message = result["messages"][-1]
    assert message.content.startswith("echo:origin")
    assert "NOTE" in message.content


def test_tool_post_deny_turns_result_into_error() -> None:
    """tool_post 的 deny 把结果替换为含原因的拒绝说明并置 error。"""
    hooks = HookSet(
        {HookPoint.TOOL_POST: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="sensitive")]}
    )
    node = _build_node([_echo_tool([])], hooks)

    result = _run_node(node, _state([_tool_call()]), make_config(hooks))

    message = result["messages"][-1]
    assert message.content == "sensitive"
    assert message.status == "error"


def test_tool_post_sees_error_on_tool_exception() -> None:
    """AC9 工具抛错时 tool_post 同样触发，is_error 为 true。"""

    @tool
    def broken(query: str) -> str:
        """Always fails."""
        raise RuntimeError("boom")

    seen: list[bool] = []
    hooks = HookSet({HookPoint.TOOL_POST: [lambda context: seen.append(context.payload.is_error)]})
    node = _build_node([broken], hooks)

    _run_node(node, _state([_tool_call(name="broken")]), make_config(hooks))

    assert seen == [True]


def test_tool_post_sees_untruncated_result() -> None:
    """AC10 tool_post 位于结果截断之内，看到完整原文。"""
    payload = "x" * 200
    seen: list[int] = []

    @tool
    def long_tool(query: str) -> str:
        """Return long content."""
        return payload

    hooks = HookSet({HookPoint.TOOL_POST: [lambda context: seen.append(len(context.payload.content))]})
    node = _build_node([long_tool], hooks, use_result_limit=True, result_limit_thrd=50)

    result = _run_node(node, _state([_tool_call(name="long_tool")]), make_config(hooks))

    assert seen == [200]
    assert result["messages"][-1].content == TOOL_RESULT_TOO_LONG_MESSAGE


def test_tool_post_reports_duration_and_tool_name() -> None:
    """tool_post 上下文含真实工具名、tool_call_id 与耗时。"""
    seen: list[dict] = []

    def handler(context):
        seen.append(
            {
                "tool_name": context.payload.tool_name,
                "tool_call_id": context.payload.tool_call_id,
                "duration_ms": context.payload.duration_ms,
            }
        )

    hooks = HookSet({HookPoint.TOOL_POST: [handler]})
    node = _build_node([_echo_tool([])], hooks)

    _run_node(node, _state([_tool_call()]), make_config(hooks))

    assert seen[0]["tool_name"] == "echo"
    assert seen[0]["tool_call_id"] == "call-1"
    assert isinstance(seen[0]["duration_ms"], int)


@tool
def search(query: str) -> str:
    """Search."""
    return query


def test_resolve_real_tool_call_unwraps_proxy_name() -> None:
    """AC10 代理解包场景下，处理器看到的工具名是真实工具名。"""
    tools_by_name = {"search": search}
    proxy_call = {"name": "tool_call", "args": {"name": "search", "arguments": {"query": "q"}}, "id": "call-9"}

    assert resolve_real_tool_call(proxy_call, tools_by_name) == ("search", {"query": "q"})


def test_run_state_absent_without_hooks() -> None:
    """未注入 hooks 时 wrapper 链形状不变，且不写运行态。"""
    node = build_tool_node(tools=[_echo_tool([])], node_options=ToolNodeSettings(use_tool_redaction=False))
    config = make_config(None)

    _run_node(node, _state([_tool_call()]), config)

    assert get_run_state(config) is None
