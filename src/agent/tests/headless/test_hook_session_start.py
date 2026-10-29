# -*- coding: utf-8 -*-
"""T-17：``session_start`` 触发与 system prompt 注入（REQ-14）。"""

from __future__ import annotations

import pytest

from tests.headless._fake_host import FakeHost, by_point
from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness


@pytest.fixture
def llm():
    with FakeLLMServer(["你好呀"]) as server:
        yield server


def _create(harness: Harness, llm: FakeLLMServer, **extra):
    params = {
        "session_code": "s1",
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
        "hooks": {"timeout_ms": 2000},
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def _session_start_payloads(requests: list[dict]) -> list[dict]:
    return [item["params"]["payload"] for item in requests if item["params"]["point"] == "session_start"]


def test_context_is_injected_before_user_input(llm):
    harness = Harness()
    _create(harness, llm)
    marker = "宿主注入的背景信息-ABC"

    with FakeHost(harness, by_point({"session_start": {"context": marker}})):
        harness.send("t1", "turn", session_code="s1", input="本轮用户问题")
        harness.wait()

    body = str(llm.requests[-1])
    assert marker in body
    assert body.index(marker) < body.index("本轮用户问题"), "context 必须位于用户输入之前"


def test_ledger_keeps_user_text_and_turn_id(llm):
    harness = Harness()
    _create(harness, llm)

    with FakeHost(harness, by_point({"session_start": {"context": "注入内容"}})):
        harness.send("t1", "turn", session_code="s1", input="原始输入")
        harness.wait()

    result = harness.result("t1")
    ledger = harness.dispatcher.registry.get("s1").ledger.records()
    assert ledger[0]["content"] == "原始输入"
    assert ledger[0]["turn_id"] == result["turn_id"]


def test_session_start_fires_every_turn_with_first_turn_flag(llm):
    harness = Harness()
    _create(harness, llm)

    with FakeHost(harness, by_point({"session_start": {"context": ""}})) as host:
        harness.send("t1", "turn", session_code="s1", input="第一轮")
        harness.wait()
        harness.send("t2", "turn", session_code="s1", input="第二轮")
        harness.wait()

    payloads = _session_start_payloads(host.requests)
    assert [item["first_turn"] for item in payloads] == [True, False]
    assert [item["input"] for item in payloads] == ["第一轮", "第二轮"]


def test_empty_context_keeps_turn_finished(llm):
    harness = Harness()
    _create(harness, llm)

    with FakeHost(harness, by_point({"session_start": {}})):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    assert llm.requests, "空 context 不得阻断本轮模型请求"


def test_resume_also_fires_session_start(llm):
    harness = Harness()
    _create(harness, llm)

    with FakeHost(harness, by_point({"session_start": {"context": "续轮上下文"}})) as host:
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()
        harness.send("r1", "resume", session_code="s1", input="继续")
        harness.wait()

    points = [item["params"]["point"] for item in host.requests]
    assert points.count("session_start") == 2
    assert harness.result("r1")["status"] == "finished"
