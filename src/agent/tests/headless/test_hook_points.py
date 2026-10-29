# -*- coding: utf-8 -*-
"""T-22：三点端到端用例与工具注入接缝（REQ-15 / REQ-16 / REQ-17）。"""

from __future__ import annotations

import pytest
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


@tool
def approval_tool(q: str) -> str:
    """需要 ITSM 审批的工具（仅用于断言审批入口不被 hook 绕开）。"""
    CALLS.append(q)
    return f"approval-result:{q}"


approval_tool.metadata = {"approval": {"enabled": True}}


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


def _create(harness: Harness, llm: FakeLLMServer, *, tools, **extra):
    params = {
        "session_code": "s1",
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
        "hooks": {"timeout_ms": 2000},
        "tools": tools,
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def _run(harness: Harness, llm: FakeLLMServer, hooks_result: dict, *, tools=None):
    _create(harness, llm, tools=tools or [fake_tool])
    with FakeHost(harness, by_point({"session_start": {"context": ""}, **hooks_result})):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()
    return harness.result("t1")


# ============================== tool_pre ==============================


def test_tool_pre_deny_skips_body_and_reports_reason(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(harness, by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "deny", "reason": "危险命令"}})):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert CALLS == [], "deny 后工具本体不得执行"
    assert harness.result("t1")["status"] == "finished"
    assert "危险命令" in str(llm.requests[-1]), "模型必须收到含 reason 的工具结果"


# ============================== tool_pre=allow 不绕开审批 ==============================


def test_tool_pre_allow_still_triggers_itsm_approval():
    with FakeLLMServer(
        ["最终回答"], tool_calls=[[{"name": "approval_tool", "args": {"q": "x"}, "id": "call_1"}]]
    ) as approval_llm:
        harness = Harness()
        _create(harness, approval_llm, tools=[approval_tool])
        with FakeHost(harness, by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "allow"}})):
            harness.send("t1", "turn", session_code="s1", input="请执行")
            harness.wait()

        result = harness.result("t1")
        assert result["status"] == "interrupt", "hook 放行后仍必须走 ITSM 审批中断"
        assert result["interrupts"][0]["reason"] == "aidev:tool_approval"
        assert CALLS == []

        # ITSM 中断本期不接线：resume 显式失败且不改图状态
        harness.send(
            "r1",
            "resume",
            session_code="s1",
            resume={"interruptId": result["interrupts"][0]["id"], "decision": "allow"},
        )
        harness.wait()
        assert harness.error("r1")["code"] == "resume_interrupt_unsupported"


# ============================== tool_post ==============================


def test_tool_post_block_hides_original_result(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(
        harness,
        by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "allow"}, "tool_post": {"decision": "block", "reason": "结果含敏感信息"}}),
    ):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    assert CALLS == ["x"], "block 不得重复执行工具本体"
    body = str(llm.requests[-1])
    assert "结果含敏感信息" in body
    assert "tool-result:x" not in body, "原结果不得进入模型上下文"


def test_tool_post_content_replaces_result(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(
        harness,
        by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "allow"}, "tool_post": {"decision": "content", "content": "改写后的结果"}}),
    ):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert CALLS == ["x"]
    body = str(llm.requests[-1])
    assert "改写后的结果" in body
    assert "tool-result:x" not in body


def test_tool_post_context_appears_in_next_model_request(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(
        harness,
        by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "allow"}, "tool_post": {"decision": "context", "context": "追加的背景补充"}}),
    ):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert CALLS == ["x"]
    body = str(llm.requests[-1])
    assert "追加的背景补充" in body
    assert "tool-result:x" in body, "context 只追加、不覆盖原结果"


# ============================== 未知裁决降级 ==============================


def test_tool_pre_unknown_decision_degrades_and_executes(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(harness, by_point({"session_start": {"context": ""}, "tool_pre": {"decision": "whatever"}})):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert CALLS == ["x"], "未知裁决按最宽松值放行"
    degraded = [
        event
        for event in harness.events("t1")
        if event["type"] == "CUSTOM" and event.get("name") == "hook_degraded"
    ]
    assert [event["value"]["reason"] for event in degraded] == ["unknown_decision"]


def test_three_points_all_fire_in_one_turn(llm):
    harness = Harness()
    _create(harness, llm, tools=[fake_tool])
    with FakeHost(
        harness,
        by_point({"session_start": {"context": "C"}, "tool_pre": {"decision": "allow"}, "tool_post": {"decision": "accept"}}),
    ) as host:
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    points = [item["params"]["point"] for item in host.requests]
    assert points == ["session_start", "tool_pre", "tool_post"]
    assert [item["id"] for item in host.requests] == ["t1#h1", "t1#h2", "t1#h3"]
    assert harness.result("t1")["status"] == "finished"
