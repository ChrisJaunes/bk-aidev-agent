# -*- coding: utf-8 -*-
"""中断续流测试：真实 ``interrupt()`` + ``Command(resume=...)`` + MemorySaver。

# AC7 / AC8 test_ask_denied_after_resume_never_executes / test_ask_allowed_after_resume_executes_once
# AC8 test_deny_and_replace_survive_resume_for_whole_batch
# AC5 / AC6 test_model_hooks_are_once_per_turn_across_resume
"""

from __future__ import annotations

from typing import Annotated, Any, List

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
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


def _new_config(hooks: HookSet | None, thread_id: str, *, resume: Any = None, input_text: str = "") -> dict:
    config: dict = {"configurable": {"thread_id": thread_id}, "callbacks": []}
    if resume is not None:
        config["configurable"]["execute_kwargs"] = ExecuteKwargs(resume=resume, input=input_text)
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
    """AC5/AC6 续流后 model_pre 仍为 1 次；model_post 终止补触发 1 次 + 最终答复 1 次。

    计数口径：本轮「最终答复触发」（AC4）与「终止补触发」（AC6）各自最多一次——中断终止
    补触发 1 次；续流后的最终答复再按 AC4 触发 1 次（``replace`` 生效）。同一轮
    ``identity.turn`` 恒定。
    """
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    pre_turns: list[int] = []
    post_calls: list[tuple[int, str]] = []

    def model_pre(context):
        pre_turns.append(context.identity.turn)

    def model_post(context):
        post_calls.append((context.identity.turn, context.payload.response.content))

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
    assert post_calls == [(1, "")]

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["允许执行"]}]}}]
    second = _new_config(hooks, "thread-turn", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)
    finalize_run(None, second)

    assert pre_turns == [1]
    # 中断终止补触发 1 次（payload 为 tool_calls 响应）+ 续流最终答复 1 次
    assert post_calls == [(1, ""), (1, "final")]
    assert set(pre_turns) == {1}


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


def test_pure_resume_consumes_terminal_post_budget() -> None:
    """纯续流（无新输入）不再重复终止补触发，但最终答复仍开放 AC4 触发。"""
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
    assert state.terminal_post_done is True
    assert state.model_post_done is False


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
            "terminal_post_done": True,
            "model_request_count": 1,
            "tool_pre_decisions": {},
        }
    }

    restore_run_state(snapshot, config)

    state = get_run_state(config)
    assert state.turn == 2
    assert state.model_pre_done is False
    assert state.model_post_done is False


def test_model_post_replace_applies_to_final_answer_after_resume(fake_llm, build_model_node_with_hooks) -> None:
    """AC4/AC6 续流后的最终答复仍触发 model_post，replace 成为会话实际输出。

    计数口径：中断终止补触发 1 次（payload 为最后一次响应，``replace`` 被忽略）；续流后的
    最终答复再触发 1 次（payload 为该最终答复，``replace`` 生效）。
    """
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    payloads: list[str] = []

    def model_post(context):
        payloads.append(context.payload.response.content)
        if context.payload.response.content == "final":
            return HookDecision(kind=HookDecisionKind.REPLACE, payload="REPLACED-FINAL")

    hooks = HookSet(
        {
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

    first = _new_config(hooks, "thread-replace")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    finalize_run(None, first)

    resume_value = [{"payload": {"answers": [{"selectedOptions": ["允许执行"]}]}}]
    second = _new_config(hooks, "thread-replace", resume=resume_value)
    graph.invoke(Command(resume=resume_value), second)
    finalize_run(None, second)

    assert payloads == ["", "final"]
    last = graph.get_state(second).values["messages"][-1]
    assert last.content == "REPLACED-FINAL"


def test_two_consecutive_turns_are_independent(fake_llm, build_model_node_with_hooks) -> None:
    """同 thread 连续两轮非续流对话：每轮 model_pre/model_post 各 1 次，覆盖视图不跨轮泄漏。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    pre_turns: list[int] = []
    post_calls: list[tuple[int, str]] = []
    marker = "OVERRIDE-TURN-1"

    def model_pre(context):
        pre_turns.append(context.identity.turn)
        if context.identity.turn == 1:
            return HookDecision(
                kind=HookDecisionKind.REPLACE,
                payload=[SystemMessage(content=marker), HumanMessage(content="replaced-1")],
            )
        return None

    def model_post(context):
        post_calls.append((context.identity.turn, context.payload.response.content))

    hooks = HookSet({HookPoint.MODEL_PRE: [model_pre], HookPoint.MODEL_POST: [model_post]})
    llm = fake_llm([AIMessage(content="one"), AIMessage(content="two")])
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-two-turns")
    graph.invoke({"messages": [HumanMessage(content="first")]}, first)
    finalize_run(None, first)

    second = _new_config(hooks, "thread-two-turns")
    graph.invoke({"messages": [HumanMessage(content="second")]}, second)
    finalize_run(None, second)

    assert pre_turns == [1, 2]
    assert post_calls == [(1, "one"), (2, "two")]

    # 第二轮请求不携带第一轮的覆盖视图（装配由测试夹具固定，此处只断言覆盖片段不泄漏）
    second_request = llm.calls[1]
    assert second_request
    assert all(getattr(message, "content", "") != marker for message in second_request)


def test_skip_with_new_input_keeps_ask_denied(fake_llm, build_model_node_with_hooks) -> None:
    """AC8 跳过提问（resume + 新输入）时挂起的 ask 未获批准，工具不执行且返回 error。"""
    calls: list[dict] = []
    tools = [_make_tool("alpha", calls)]
    pre_turns: list[int] = []
    post_calls: list[tuple[int, str]] = []

    def model_pre(context):
        pre_turns.append(context.identity.turn)

    def model_post(context):
        post_calls.append((context.identity.turn, context.payload.response.content))

    hooks = HookSet(
        {
            HookPoint.MODEL_PRE: [model_pre],
            HookPoint.MODEL_POST: [model_post],
            HookPoint.TOOL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.ASK, reason="need approval")],
        }
    )
    llm = fake_llm(
        [
            AIMessage(content="", tool_calls=[_tool_call("call-1", "alpha")]),
            AIMessage(content="skipped-final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-skip")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first)
    finalize_run(None, first)

    # 跳过提问：resume 答案为空 + 新用户输入
    resume_value = [{"payload": {"answers": []}}]
    second = _new_config(hooks, "thread-skip", resume=resume_value, input_text="换个问题")
    graph.invoke(
        Command(resume=resume_value, update={"messages": [HumanMessage(content="换个问题")]}),
        second,
    )

    assert calls == []
    tool_messages = [
        message for message in graph.get_state(second).values["messages"] if isinstance(message, ToolMessage)
    ]
    assert tool_messages
    assert tool_messages[-1].status == "error"
    assert "hook ask not approved" in tool_messages[-1].content

    # 新一轮：model_pre 按新轮触发 1 次，turn 递增
    assert pre_turns == [1, 2]
    assert post_calls[-1][1] == "skipped-final"


def test_skip_with_new_input_keeps_deny_and_replace(fake_llm, build_model_node_with_hooks) -> None:
    """AC8 跳过提问换轮时，同批次挂起的 deny / replace 裁决仍按 tool_call_id 生效。"""
    alpha_calls: list[dict] = []
    beta_calls: list[dict] = []
    gamma_calls: list[dict] = []
    tools = [
        _make_tool("alpha", alpha_calls),
        _make_tool("beta", beta_calls),
        _make_tool("gamma", gamma_calls),
    ]

    def tool_pre(context):
        if context.payload.tool_name == "alpha":
            return HookDecision(kind=HookDecisionKind.REPLACE, payload={"query": "rewritten"})
        if context.payload.tool_name == "beta":
            return HookDecision(kind=HookDecisionKind.DENY, reason="blocked")
        return HookDecision(kind=HookDecisionKind.ASK, reason="need approval")

    hooks = HookSet({HookPoint.TOOL_PRE: [tool_pre]})
    llm = fake_llm(
        [
            AIMessage(
                content="",
                tool_calls=[
                    _tool_call("call-1", "alpha"),
                    _tool_call("call-2", "beta"),
                    _tool_call("call-3", "gamma"),
                ],
            ),
            AIMessage(content="skipped-final"),
        ]
    )
    graph = _build_graph(build_model_node_with_hooks(llm, hooks=hooks, tools=tools), tools, hooks)

    first = _new_config(hooks, "thread-skip-batch")
    graph.invoke({"messages": [HumanMessage(content="hello")]}, first)
    assert _interrupts(graph, first)
    finalize_run(None, first)

    resume_value = [{"payload": {"answers": []}}]
    second = _new_config(hooks, "thread-skip-batch", resume=resume_value, input_text="换个问题")
    graph.invoke(
        Command(resume=resume_value, update={"messages": [HumanMessage(content="换个问题")]}),
        second,
    )

    assert alpha_calls == [{"name": "alpha", "query": "rewritten"}]
    assert beta_calls == []
    assert gamma_calls == []
