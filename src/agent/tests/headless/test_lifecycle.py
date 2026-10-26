# -*- coding: utf-8 -*-
"""T-7：``resume`` / ``close`` 与会话生命周期（REQ-6 / REQ-9）。"""

import pytest
from ag_ui.core import EventType

from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness


@pytest.fixture
def llm():
    with FakeLLMServer(["第一轮回答", "第二轮回答", "第三轮回答"]) as server:
        yield server


def _create(harness: Harness, llm: FakeLLMServer, session_code="s1", request_id="c1"):
    harness.send_obj(
        request_id,
        "create",
        {
            "session_code": session_code,
            "agent": {"agent_code": "agent-hook"},
            "llm": {"base_url": llm.base_url, "model": "fake-model"},
        },
    )
    return harness.result(request_id)


def test_resume_continues_same_session_with_history(llm):
    harness = Harness()
    _create(harness, llm)
    harness.send("t1", "turn", session_code="s1", input="第一轮问题")
    harness.wait()
    harness.send("r1", "resume", session_code="s1", input="第二轮问题")
    harness.wait()

    result = harness.result("r1")
    assert result["session_code"] == "s1"
    assert result["status"] == "finished"

    # 同一 thread_id：第二次模型请求包含第一轮历史
    assert len(llm.requests) >= 2
    second_body = str(llm.requests[1])
    assert "第一轮问题" in second_body and "第一轮回答" in second_body
    assert "第二轮问题" in second_body

    ledger = harness.dispatcher.registry.get("s1").ledger.records()
    assert [record["content"] for record in ledger] == [
        "第一轮问题",
        "第一轮回答",
        "第二轮问题",
        "第二轮回答",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        {"interruptId": "i-1", "status": "resolved", "payload": {"answers": []}},
        [{"interruptId": "i-1"}],
    ],
)
def test_interrupt_payload_without_pending_is_rejected(llm, payload):
    """agent-hook 第二期：中断恢复已接线，但 interruptId 未命中 pending 时显式失败（REQ-17）。

    一期「headless 不支持中断恢复」的约束由 REQ-17 取代；恢复语义的正例见
    ``test_hook_interrupt.py``。
    """
    harness = Harness()
    _create(harness, llm)
    harness.send_obj("r1", "resume", {"session_code": "s1", "input": "x", "resume": payload})
    harness.wait()

    error = harness.error("r1")
    assert error["code"] == "resume_interrupt_not_found"
    types = harness.event_types("r1")
    assert types[-1] == EventType.RUN_ERROR.value
    # 失败轮不写账本
    assert harness.dispatcher.registry.get("s1").ledger.records() == []


def test_close_rejects_later_requests_for_that_session_only(llm):
    harness = Harness()
    _create(harness, llm, session_code="s1", request_id="c1")
    _create(harness, llm, session_code="s2", request_id="c2")
    harness.send("x1", "close", session_code="s1")
    assert harness.result("x1") == {"session_code": "s1", "closed": True, "already_closed": False}

    harness.send("t1", "turn", session_code="s1", input="hi")
    harness.wait()
    assert harness.error("t1")["code"] == "session.closed"
    assert harness.event_types("t1")[-1] == EventType.RUN_ERROR.value

    # 其他会话不受影响
    harness.send("t2", "turn", session_code="s2", input="hi")
    harness.wait()
    assert harness.result("t2")["status"] == "finished"


def test_session_code_is_echoed_in_every_response(llm):
    harness = Harness()
    assert _create(harness, llm)["session_code"] == "s1"
    harness.send("t1", "turn", session_code="s1", input="hi")
    harness.wait()
    assert harness.result("t1")["session_code"] == "s1"
    harness.send("r1", "resume", session_code="s1", input="again")
    harness.wait()
    assert harness.result("r1")["session_code"] == "s1"
    harness.send("x1", "close", session_code="s1")
    assert harness.result("x1")["session_code"] == "s1"


def test_resume_unknown_session_reports_not_found():
    harness = Harness()
    harness.send("r1", "resume", session_code="ghost", input="hi")
    harness.wait()
    assert harness.error("r1")["code"] == "session.not_found"
