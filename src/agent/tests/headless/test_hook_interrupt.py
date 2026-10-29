# -*- coding: utf-8 -*-
"""T-20 / T-21：``ask`` 中断、``status=interrupt`` 收尾与 headless 恢复（REQ-17）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.hooks import HOOK_ASK_REASON
from langchain_core.tools import tool

from tests.headless._fake_host import FakeHost, by_point
from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness

CALLS: list[str] = []


@tool
def fake_tool(q: str) -> str:
    """记录调用次数并返回结果。"""
    CALLS.append(q)
    return f"tool-result:{q}"


@pytest.fixture(autouse=True)
def _reset_calls():
    CALLS.clear()
    yield
    CALLS.clear()


@pytest.fixture
def llm():
    with FakeLLMServer(
        ["最终回答"],
        tool_calls=[[{"name": "fake_tool", "args": {"q": "x"}, "id": "call_1"}]],
    ) as server:
        yield server


HOOK_RESULTS = {
    "session_start": {"context": ""},
    "tool_pre": {"decision": "ask", "reason": "请人工确认"},
    "tool_post": {"decision": "accept"},
}


def _create(harness: Harness, llm: FakeLLMServer, **extra):
    params = {
        "session_code": "s1",
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
        "hooks": {"timeout_ms": 2000},
        "tools": [fake_tool],
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def _run_to_interrupt(harness: Harness, llm: FakeLLMServer) -> tuple[dict, list[dict]]:
    _create(harness, llm)
    with FakeHost(harness, by_point(HOOK_RESULTS)) as host:
        harness.send("t1", "turn", session_code="s1", input="帮我执行")
        harness.wait()
        result = harness.result("t1")
        requests = list(host.requests)
    return result, requests


# ============================== 中断轮 ==============================


def test_ask_interrupt_yields_status_interrupt_with_tool_call_id(llm):
    harness = Harness()
    result, requests = _run_to_interrupt(harness, llm)

    assert result["status"] == "interrupt"
    assert result["session_code"] == "s1"
    assert CALLS == [], "ask 不得执行工具本体"
    (interrupt,) = result["interrupts"]
    assert interrupt["reason"] == HOOK_ASK_REASON
    assert interrupt["toolCallId"] == "call_1"
    assert interrupt["id"]

    # 事件侧仍以 RUN_FINISHED(outcome=interrupt) 收尾，不新增事件类型
    finished = [
        event for event in harness.events("t1") if event["type"] == "RUN_FINISHED"
    ]
    assert finished and finished[-1]["outcome"]["type"] == "interrupt"
    assert finished[-1]["outcome"]["interrupts"][0]["toolCallId"] == "call_1"
    # 重放前 tool_pre 只上报一次
    assert [item["params"]["point"] for item in requests].count("tool_pre") == 1


def test_turn_after_pending_interrupt_returns_structured_error(llm):
    harness = Harness()
    _run_to_interrupt(harness, llm)

    harness.send("t2", "turn", session_code="s1", input="继续")
    harness.wait()

    assert harness.error("t2")["code"] == "session.pending_interrupt"


# ============================== 恢复 ==============================


def test_resume_allow_executes_tool_and_finishes(llm):
    harness = Harness()
    result, requests = _run_to_interrupt(harness, llm)
    interrupt_id = result["interrupts"][0]["id"]

    with FakeHost(harness, by_point(HOOK_RESULTS)) as host:
        harness.send(
            "r1",
            "resume",
            session_code="s1",
            resume={"interruptId": interrupt_id, "toolCallId": "call_1", "decision": "allow"},
        )
        harness.wait()

    assert harness.result("r1")["status"] == "finished"
    assert CALLS == ["x"], "放行后工具必须执行且只执行一次"
    # 重放不得再次上报同一 tool_pre
    assert [item["params"]["point"] for item in host.requests].count("tool_pre") == 0
    assert "tool-result:x" in str(llm.requests[-1])


def test_resume_deny_skips_tool_and_reports_reason(llm):
    harness = Harness()
    result, _ = _run_to_interrupt(harness, llm)
    interrupt_id = result["interrupts"][0]["id"]

    with FakeHost(harness, by_point(HOOK_RESULTS)):
        harness.send("r1", "resume", session_code="s1", resume={"interruptId": interrupt_id, "decision": "deny"})
        harness.wait()

    assert harness.result("r1")["status"] == "finished"
    assert CALLS == [], "拒绝后工具不得执行"
    assert "hook 人工裁决未放行" in str(llm.requests[-1])


def test_resume_unknown_interrupt_id_then_correct_one(llm):
    harness = Harness()
    result, _ = _run_to_interrupt(harness, llm)
    interrupt_id = result["interrupts"][0]["id"]

    harness.send("r1", "resume", session_code="s1", resume={"interruptId": "int-does-not-exist", "decision": "allow"})
    harness.wait()
    assert harness.error("r1")["code"] == "resume_interrupt_not_found"
    assert CALLS == []

    with FakeHost(harness, by_point(HOOK_RESULTS)):
        harness.send("r2", "resume", session_code="s1", resume={"interruptId": interrupt_id, "decision": "allow"})
        harness.wait()

    assert harness.result("r2")["status"] == "finished"
    assert CALLS == ["x"]


def test_resume_payload_without_interrupt_id_is_rejected(llm):
    harness = Harness()
    _run_to_interrupt(harness, llm)

    harness.send("r1", "resume", session_code="s1", resume={"decision": "allow"})
    harness.wait()

    assert harness.error("r1")["code"] == "resume_payload_unsupported"


def test_interrupt_round_writes_assistant_text_to_ledger(llm):
    harness = Harness()
    result, _ = _run_to_interrupt(harness, llm)
    interrupt_id = result["interrupts"][0]["id"]

    # 用户原文与 turn_id 不变：中断轮不得改写账本里的 user 记录
    ledger = harness.dispatcher.registry.get("s1").ledger.records()
    assert ledger[0]["role"] == "user"
    assert ledger[0]["content"] == "帮我执行"

    with FakeHost(harness, by_point(HOOK_RESULTS)):
        harness.send("r1", "resume", session_code="s1", resume={"interruptId": interrupt_id, "decision": "allow"})
        harness.wait()

    # 恢复轮把已产出的 assistant 文本写入账本
    records = harness.dispatcher.registry.get("s1").ledger.records()
    assert [record["role"] for record in records] == ["user", "assistant"]
    assert records[-1]["content"] == "最终回答"
