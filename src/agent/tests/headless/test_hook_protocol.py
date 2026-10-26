# -*- coding: utf-8 -*-
"""T-18：协议两类新行与答复分流（REQ-18）。"""

from __future__ import annotations

import json

import pytest
from aidev_agent.headless.protocol import is_hook_reply

from tests.headless._fake_host import FakeHost, allow_all
from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness

HOOKS = {"timeout_ms": 5000}


@pytest.fixture
def llm():
    with FakeLLMServer(["你好呀"]) as server:
        yield server


def _create(harness: Harness, llm: FakeLLMServer, **extra):
    params = {
        "session_code": "s1",
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


# ============================== 协议面 ==============================


def test_is_hook_reply_discriminates_reply_from_request():
    assert is_hook_reply({"id": "t#h1", "ok": True, "result": {}}) is True
    assert is_hook_reply({"id": "t1", "method": "turn", "params": {}}) is False
    assert is_hook_reply({"id": "t1", "ok": True, "method": "hook"}) is False
    assert is_hook_reply("not-a-dict") is False


def test_hook_request_line_shape_and_id_derivation(llm):
    harness = Harness()
    _create(harness, llm, hooks=HOOKS)

    with FakeHost(harness, allow_all()) as host:
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    requests = host.hook_requests()
    assert requests, "启用 hooks 后必须出现 type=request 的 hook 请求行"
    first = requests[0]
    assert first["id"] == "t1#h1"
    assert first["method"] == "hook"
    assert first["params"]["point"] == "session_start"
    assert first["params"]["session_code"] == "s1"
    assert first["params"]["turn_id"]
    assert set(first["params"]["payload"]) == {"session_code", "turn_id", "first_turn", "input"}
    assert first["params"]["payload"]["first_turn"] is True


def test_reply_line_produces_no_response_and_no_unknown_method(llm):
    harness = Harness()
    _create(harness, llm, hooks=HOOKS)

    with FakeHost(harness, allow_all()):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    lines = harness.lines()
    reply_ids = {line.get("id") for line in lines if "ok" in line and line.get("type") != "event"}
    assert "t1#h1" not in reply_ids, "答复行不得产生响应行"
    assert not any(line.get("error", {}).get("code") == "protocol.unknown_method" for line in lines)
    assert harness.result("t1")["status"] == "finished"


def test_second_turn_continues_hook_sequence(llm):
    harness = Harness()
    _create(harness, llm, hooks=HOOKS)

    with FakeHost(harness, allow_all()) as host:
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()
        harness.send("t2", "turn", session_code="s1", input="再来")
        harness.wait()

    ids = [line["id"] for line in host.requests]
    assert ids == ["t1#h1", "t2#h2"], ids


def test_unknown_and_malformed_reply_lines_do_not_break_process(llm):
    harness = Harness()
    _create(harness, llm, hooks=HOOKS)

    # 未命中任何等待方的答复行 + 无 id 的答复行 + 畸形 JSON：都不应报错、不应退出
    harness.send_raw(json.dumps({"id": "nobody#h9", "ok": True, "result": {}}))
    harness.send_raw(json.dumps({"ok": True, "result": {}}))
    harness.send_raw("{not json")

    with FakeHost(harness, allow_all()):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    # 进程仍可用：下一条合法请求正常处理
    harness.send("t2", "turn", session_code="s1", input="继续")
    harness.wait()
    assert harness.result("t2")["status"] == "finished"


def test_every_stdout_line_is_parseable_while_hook_in_flight(llm):
    harness = Harness()
    _create(harness, llm, hooks=HOOKS)

    with FakeHost(harness, allow_all()):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    for line in harness.lines():
        assert isinstance(line, dict), "stdout 每行都应是完整 JSON 对象（写锁未交错）"
