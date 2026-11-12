# -*- coding: utf-8 -*-
"""模型点位测试：session_start、model_pre、model_post 与运行收尾。

# AC1 test_session_start_runs_once_before_model_pre / test_session_start_append_persists_in_later_requests
# AC2 test_session_start_deny_stops_before_any_request
# AC3 test_model_pre_replace_becomes_actual_request / test_model_pre_replace_is_inherited_by_later_requests
# AC4 test_model_post_replace_becomes_turn_output
# AC5 test_model_pre_and_post_once_per_turn
# AC6 test_finalize_run_triggers_model_post_without_final_answer
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aidev_agent.core.hook import (
    HookDecision,
    HookDecisionKind,
    HookDeniedError,
    HookPoint,
    HookSet,
)
from aidev_agent.core.hook.run_state import HOOK_RUN_STATE_KEY, finalize_run, get_run_state

from .conftest import make_config, run_node

TOOL_CALL_RESPONSE = AIMessage(
    content="",
    tool_calls=[{"name": "search", "args": {"query": "x"}, "id": "call-1"}],
)


def _append_handler(fragment: str, calls: list[str]):
    def handler(context):
        calls.append(context.point.value)
        return HookDecision(kind=HookDecisionKind.APPEND, payload=fragment)

    return handler


def test_session_start_runs_once_before_model_pre(fake_llm, build_model_node_with_hooks) -> None:
    """AC1 session_start 在首轮 model_pre 之前恰好一次，append 进入实际发送内容。"""
    order: list[str] = []

    def session_start(context):
        order.append(HookPoint.SESSION_START.value)
        return HookDecision(kind=HookDecisionKind.APPEND, payload="EXTRA-NOTE")

    def model_pre(context):
        order.append(HookPoint.MODEL_PRE.value)
        return None

    hooks = HookSet({HookPoint.SESSION_START: [session_start], HookPoint.MODEL_PRE: [model_pre]})
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)
    config = make_config(hooks)

    run_node(node, config)

    assert order == ["session_start", "model_pre"]
    sent = llm.calls[0]
    system_texts = [message.content for message in sent if isinstance(message, SystemMessage)]
    assert "EXTRA-NOTE" in system_texts


def test_session_start_append_persists_in_later_requests(fake_llm, build_model_node_with_hooks) -> None:
    """AC1 append 片段在该次运行后续请求中保持存在。"""
    hooks = HookSet({HookPoint.SESSION_START: [_append_handler("EXTRA-NOTE", [])]})
    llm = fake_llm([TOOL_CALL_RESPONSE, AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)
    config = make_config(hooks)
    messages = [HumanMessage(content="hello")]

    run_node(node, config, messages)
    run_node(node, config, messages)

    assert len(llm.calls) == 2
    for sent in llm.calls:
        assert any(isinstance(item, SystemMessage) and item.content == "EXTRA-NOTE" for item in sent)


def test_session_start_deny_stops_before_any_request(fake_llm, build_model_node_with_hooks) -> None:
    """AC2 session_start 返回 deny 时不发起任何模型请求，以可读错误结束。"""
    hooks = HookSet(
        {HookPoint.SESSION_START: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="not allowed")]}
    )
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)

    with pytest.raises(HookDeniedError) as error:
        run_node(node, make_config(hooks))

    assert llm.calls == []
    assert "hook deny at session_start: not allowed" in str(error.value)


def test_model_pre_replace_becomes_actual_request(fake_llm, build_model_node_with_hooks) -> None:
    """AC3 model_pre 的 replace 与该次实际发送的消息逐项相同。"""
    replacement = [SystemMessage(content="SYS"), HumanMessage(content="REPLACED")]

    def model_pre(context):
        return HookDecision(kind=HookDecisionKind.REPLACE, payload=replacement)

    hooks = HookSet({HookPoint.MODEL_PRE: [model_pre]})
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)

    run_node(node, make_config(hooks))

    assert [item.content for item in llm.calls[0]] == ["SYS", "REPLACED"]


def test_model_pre_replace_is_inherited_by_later_requests(fake_llm, build_model_node_with_hooks) -> None:
    """AC3 覆盖结果在该轮后续请求中保持继承（工具回灌后仍带着覆盖内容）。"""
    replacement = [SystemMessage(content="SYS"), HumanMessage(content="REPLACED")]

    def model_pre(context):
        return HookDecision(kind=HookDecisionKind.REPLACE, payload=replacement)

    hooks = HookSet({HookPoint.MODEL_PRE: [model_pre]})
    llm = fake_llm([TOOL_CALL_RESPONSE, AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)
    config = make_config(hooks)
    messages = [HumanMessage(content="hello")]

    run_node(node, config, messages)
    run_node(node, config, messages)

    assert len(llm.calls) == 2
    for sent in llm.calls:
        assert [item.content for item in sent][:2] == ["SYS", "REPLACED"]


def test_model_pre_deny_blocks_request(fake_llm, build_model_node_with_hooks) -> None:
    """model_pre 返回 deny 时不发出该请求，运行以可读错误结束。"""
    hooks = HookSet({HookPoint.MODEL_PRE: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="blocked")]})
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)

    with pytest.raises(HookDeniedError):
        run_node(node, make_config(hooks))

    assert llm.calls == []


def test_model_pre_and_post_once_per_turn(fake_llm, build_model_node_with_hooks) -> None:
    """AC5 一轮多次请求时 model_pre / model_post 各只触发一次，中间响应不触发 post。"""
    pre_calls: list = []
    post_calls: list = []

    def model_pre(context):
        pre_calls.append(context.identity.turn)

    def model_post(context):
        post_calls.append(context.payload.response.content)

    hooks = HookSet({HookPoint.MODEL_PRE: [model_pre], HookPoint.MODEL_POST: [model_post]})
    llm = fake_llm([TOOL_CALL_RESPONSE, AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)
    config = make_config(hooks)
    messages = [HumanMessage(content="hello")]

    run_node(node, config, messages)
    assert post_calls == []

    run_node(node, config, messages)

    assert pre_calls == [1]
    assert post_calls == ["final"]


def test_model_post_replace_becomes_turn_output(fake_llm, build_model_node_with_hooks) -> None:
    """AC4 model_post 的 replace 文本成为本轮实际输出。"""
    hooks = HookSet(
        {HookPoint.MODEL_POST: [lambda context: HookDecision(kind=HookDecisionKind.REPLACE, payload="REPLACED-FINAL")]}
    )
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)

    result = run_node(node, make_config(hooks))

    assert result["messages"][0].content == "REPLACED-FINAL"


def test_model_post_deny_terminates_run(fake_llm, build_model_node_with_hooks) -> None:
    """model_post 返回 deny 时以可读错误终止本次运行。"""
    hooks = HookSet(
        {HookPoint.MODEL_POST: [lambda context: HookDecision(kind=HookDecisionKind.DENY, reason="rejected")]}
    )
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm, hooks=hooks)

    with pytest.raises(HookDeniedError):
        run_node(node, make_config(hooks))


def test_finalize_run_triggers_model_post_without_final_answer(fake_llm, build_model_node_with_hooks) -> None:
    """AC6 无最终答复的终止路径补触发一次 model_post，且不重复触发。"""
    post_calls: list = []

    def model_post(context):
        post_calls.append(context.payload.response.content)
        return HookDecision(kind=HookDecisionKind.REPLACE, payload="ignored")

    hooks = HookSet({HookPoint.MODEL_POST: [model_post]})
    llm = fake_llm([AIMessage(content="", tool_calls=TOOL_CALL_RESPONSE.tool_calls)])
    node = build_model_node_with_hooks(llm, hooks=hooks)
    config = make_config(hooks)

    run_node(node, config)
    assert post_calls == []

    finalize_run(None, config)
    finalize_run(None, config)

    assert len(post_calls) == 1


def test_no_hooks_keeps_run_state_absent(fake_llm, build_model_node_with_hooks) -> None:
    """未注入 hooks 时模型链不写运行态，行为与基线一致。"""
    llm = fake_llm([AIMessage(content="final")])
    node = build_model_node_with_hooks(llm)
    config = make_config(None)

    result = run_node(node, config)

    assert config["configurable"].get(HOOK_RUN_STATE_KEY) is None
    assert get_run_state(config) is None
    assert result["messages"][0].content == "final"
