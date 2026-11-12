# -*- coding: utf-8 -*-
"""中断续流测试：真实 ``interrupt()`` + ``Command(resume=...)`` + MemorySaver。

# AC7 / AC8 test_ask_denied_after_resume_never_executes / test_ask_allowed_after_resume_executes_once
# AC8 test_deny_and_replace_survive_resume_for_whole_batch
# AC5 / AC6 test_model_hooks_are_once_per_turn_across_resume
"""

from __future__ import annotations

from typing import Annotated, Any, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command
from typing_extensions import TypedDict

from aidev_agent.core.graphs.react.graph import _send_dispatch_route
from aidev_agent.core.hook import HookDecision, HookDecisionKind, HookPoint, HookSet
from aidev_agent.core.hook.run_state import (
    HOOK_STATE_CHANNEL,
    bind_run_state,
    finalize_run,
    get_run_state,
    restore_run_state,
)
from aidev_agent.core.hook.runner import HookRunner
from aidev_agent.core.hook.stages import run_tool_pre_stage
from aidev_agent.core.nodes.pv import make_pv_node
from aidev_agent.core.nodes.tool import ToolNodeSettings, build_tool_node
from aidev_agent.pydantic_models import ExecuteKwargs


class ResumeState(TypedDict):
    messages: Annotated[List, add_messages]
    hook_run_state: dict


def _route(state: dict) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "pv_node"
    return END


def _new_config(hooks: HookSet | None, thread_id: str, *, resume: Any = None) -> dict:
    config: dict = {"configurable": {"thread_id": thread_id}, "callbacks": []}
    if resume is not None:
        config["configurable"]["execute_kwargs"] = ExecuteKwargs(resume=resume)
    bind_run_state(config, hooks, agent_code="agent", session_code=thread_id, run_id="run-1")
    return config


def _build_graph(model_node, tools: list, hooks: HookSet):
    pv_node = make_pv_node(
        client=None,
        app_code="",
        resource_manager=None,
        before_dispatch=lambda state, config: run_tool_pre_stage(state, config, tools),
    )
    tool_node = build_tool_node(
        tools=tools,
        node_options=ToolNodeSettings(
            use_tool_redaction=False,
            use_tool_untrusted_sanitize=False,
            use_json_repair_on_error=False,
            enable_tool_loop_guard=False,
        ),
        hooks=HookRunner(hooks),
    )
    graph = StateGraph(ResumeState)
    graph.add_node("model", model_node)
    graph.add_node("pv_node", pv_node)
    graph.add_node("tools", tool_node)
    graph.add_edge(START, "model")
    graph.add_conditional_edges("model", _route, {"pv_node": "pv_node", END: END})
    graph.add_conditional_edges("pv_node", _send_dispatch_route)
    graph.add_edge("tools", "model")
    return graph.compile(checkpointer=MemorySaver())


def _make_tool(name: str, calls: list[dict]):
    @tool(name)
    def _tool(query: str) -> str:
        """Echo the query."""
        calls.append({"name": name, "query": query})
        return f"{name}:{query}"

    return _tool


def _tool_call(tool_call_id: str, name: str, query: str = "origin") -> dict:
    return {"name": name, "args": {"query": query}, "id": tool_call_id}


def _interrupts(graph, config) -> list:
    return [task for task in graph.get_state(config).tasks if task.interrupts]


def test_ask_denied_after_resume_never_executes(fake_llm, build_model_node_with_hooks) -> None:
    """AC8 真实 interrupt 后人工选择拒绝：工具执行 0 次，模型收到 status=error 的拒绝结果。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    seen: list[str] = []

    def tool_pre(context):
        seen.append(context.payload.tool_call_id)
        return HookDecision(kind=HookDecisionKind.ASK, reason="need approval")

    hooks = HookSet({HookPoint.TOOL_PRE: [tool_pre]})
    llm = fake_llm(
        [
            AIMessage(content="", tool_calls=[_tool_call("call-1", "alpha")]),
            AIMessage(content="final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-deny")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first), "首次运行应在 tool_pre ask 处中断"
    finalize_run(None, first)

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["拒绝执行"]}]}}]
    second = _new_config(hooks, "thread-deny", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)

    assert calls == []
    assert seen == ["call-1"]
    messages = graph.get_state(second).values["messages"]
    tool_messages = [message for message in messages if isinstance(message, ToolMessage)]
    assert len(tool_messages) == 1
    assert tool_messages[0].status == "error"
    assert "hook ask not approved" in tool_messages[0].content


def test_ask_allowed_after_resume_executes_once(fake_llm, build_model_node_with_hooks) -> None:
    """AC8 真实 interrupt 后人工选择允许：工具执行 1 次。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    hooks = HookSet(
        {HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.ASK, reason="need approval")]}
    )
    llm = fake_llm(
        [
            AIMessage(content="", tool_calls=[_tool_call("call-1", "alpha")]),
            AIMessage(content="final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-allow")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first)
    finalize_run(None, first)

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["允许执行"]}]}}]
    second = _new_config(hooks, "thread-allow", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)

    assert calls == [{"name": "alpha", "query": "origin"}]


def test_deny_and_replace_survive_resume_for_whole_batch(fake_llm, build_model_node_with_hooks) -> None:
    """AC8 同批次 deny / replace 裁决在中断续流后仍生效。"""
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    tools = [_make_tool("alpha", alpha_calls), _make_tool("beta", beta_calls)]

    def tool_pre(context):
        if context.payload.tool_name == "alpha":
            return HookDecision(kind=HookDecisionKind.REPLACE, payload={"query": "rewritten"})
        return HookDecision(kind=HookDecisionKind.ASK, reason="need approval")

    hooks = HookSet({HookPoint.TOOL_PRE: [tool_pre]})
    llm = fake_llm(
        [
            AIMessage(
                content="",
                tool_calls=[_tool_call("call-1", "alpha"), _tool_call("call-2", "beta")],
            ),
            AIMessage(content="final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-batch")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first)
    persisted = graph.get_state(first).values[HOOK_STATE_CHANNEL]["tool_pre_decisions"]
    assert persisted["call-1"]["kind"] == HookDecisionKind.REPLACE.value
    assert persisted["call-2"]["kind"] == HookDecisionKind.ASK.value
    finalize_run(None, first)

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["拒绝执行"]}]}}]
    second = _new_config(hooks, "thread-batch", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)

    assert alpha_calls == [{"name": "alpha", "query": "rewritten"}]
    assert beta_calls == []


def test_model_hooks_are_once_per_turn_across_resume(fake_llm, build_model_node_with_hooks) -> None:
    """AC5/AC6 续流后 model_pre 仍为 1 次；model_post 整轮 1 次，identity.turn 不变。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    pre_turns: list[int] = []
    post_turns: list[int] = []

    def model_pre(context):
        pre_turns.append(context.identity.turn)

    def model_post(context):
        post_turns.append(context.identity.turn)

    hooks = HookSet(
        {
            HookPoint.MODEL_PRE: [model_pre],
            HookPoint.MODEL_POST: [model_post],
            HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.ASK, reason="ask")],
        }
    )
    llm = fake_llm(
        [
            AIMessage(content="", tool_calls=[_tool_call("call-1", "alpha")]),
            AIMessage(content="final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-turn")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first)
    # 生产出口：中断终止按 AC6 补触发一次 model_post
    finalize_run(None, first)
    assert pre_turns == [1]
    assert post_turns == [1]

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["允许执行"]}]}}]
    second = _new_config(hooks, "thread-turn", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)
    finalize_run(None, second)

    assert pre_turns == [1]
    assert post_turns == [1]
    assert set(pre_turns + post_turns) == {1}


def test_started_without_resume_still_fires_model_post(fake_llm, build_model_node_with_hooks) -> None:
    """无中断的正常运行仍按 AC4 触发 model_post（续流抑制不影响正常路径）。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    post_messages: list[str] = []
    hooks = HookSet({HookPoint.MODEL_POST: [lambda context: post_messages.append(context.payload.response.content)]})
    llm = fake_llm([AIMessage(content="final")])
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    config = _new_config(hooks, "thread-plain")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config)
    finalize_run(None, config)

    assert post_messages == ["final"]


@pytest.mark.parametrize("point", [HookPoint.TOOL_PRE])
def test_resume_keeps_hook_channel_in_state(point, fake_llm, build_model_node_with_hooks) -> None:
    """续流快照写入图状态通道，未注入 hooks 时该通道不出现。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    hooks = HookSet({point: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="no")]})
    llm = fake_llm([AIMessage(content="", tool_calls=[_tool_call("call-1", "alpha")]), AIMessage(content="final")])
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    config = _new_config(hooks, "thread-channel")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, config)

    assert HOOK_STATE_CHANNEL in graph.get_state(config).values


def test_pure_resume_consumes_model_post_budget() -> None:
    """纯续流（无新输入）不再重复触发 model_post，轮序号保持。"""
    hooks = HookSet({HookPoint.MODEL_POST: [lambda context: None]})
    config = _new_config(hooks, "thread-unit", resume=[{"payload": {"answers": []}}])
    snapshot = {
        HOOK_STATE_CHANNEL: {
            "turn": 1,
            "model_pre_done": True,
            "model_post_done": False,
            "model_request_count": 1,
            "tool_pre_decisions": {},
        }
    }

    restore_run_state(snapshot, config)

    state = get_run_state(config)
    assert state.turn == 1
    assert state.model_pre_done is True
    assert state.model_post_done is True


def test_resume_with_new_input_opens_new_turn() -> None:
    """跳过提问并输入新消息：轮序号 +1，重新开放 model_pre / model_post。"""
    hooks = HookSet({HookPoint.MODEL_POST: [lambda context: None]})
    config = _new_config(hooks, "thread-turn-unit", resume=[{"payload": {"answers": []}}])
    config["configurable"]["execute_kwargs"] = ExecuteKwargs(resume=[{"payload": {"answers": []}}], input="新消息")
    snapshot = {
        HOOK_STATE_CHANNEL: {
            "turn": 1,
            "model_pre_done": True,
            "model_post_done": True,
            "model_request_count": 1,
            "tool_pre_decisions": {},
        }
    }

    restore_run_state(snapshot, config)

    state = get_run_state(config)
    assert state.turn == 2
    assert state.model_pre_done is False
    assert state.model_post_done is False
