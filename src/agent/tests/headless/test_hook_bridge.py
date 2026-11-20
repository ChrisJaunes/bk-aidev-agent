# -*- coding: utf-8 -*-
"""headless hook 桥：把进程内 ``HookSet`` 接成宿主 NDJSON 往返的端到端用例。

覆盖：
- 关闭态：不传 ``hooks`` 时不产生任何 ``type=request`` 行；
- ``turn_start``：宿主返回 ``context`` → 本轮 system prompt 含该文本；
- ``tool_pre``：``deny`` 时工具零执行、模型收到 reason；``allow`` 时正常执行；
- ``tool_post``：``context`` 追加到模型可见结果；
- 降级：宿主不答复 → 超时 fail-open，写恰好一条 ``hook_degraded``；
- 协议形状：请求行 ``id`` 为 ``<请求 id>#h<n>``，``params`` 四键齐备。
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from tests.headless._fake_host import FakeHost, allow_all, by_point, silent
from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness

CALLS: list[str] = []


@tool
def probe_tool(q: str) -> str:
    """记录调用并返回结果。"""
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
        tool_calls=[[{"name": "probe_tool", "args": {"q": "x"}, "id": "call_1"}]],
    ) as server:
        yield server


def _create(harness: Harness, llm: FakeLLMServer, **extra):
    params = {
        "session_code": "s1",
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
        "tools": [probe_tool],
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def _run(harness: Harness, llm: FakeLLMServer, results: dict, *, hooks=None):
    _create(harness, llm, hooks=hooks or {"timeout_ms": 2000})
    with FakeHost(harness, by_point(results)) as host:
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()
    return harness.result("t1"), host


# ============================== 关闭态 ==============================


def test_hooks_disabled_emits_no_request_lines(llm):
    harness = Harness()
    _create(harness, llm)
    harness.send("t1", "turn", session_code="s1", input="你好")
    harness.wait()

    assert [line for line in harness.lines() if line.get("type") == "request"] == []
    assert harness.result("t1")["status"] == "finished"


# ============================== turn_start ==============================


def test_turn_start_context_reaches_system_prompt(llm):
    harness = Harness()
    marker = "宿主注入的背景-ABC"
    with FakeHost(harness, by_point({"turn_start": {"context": marker}, "tool_pre": {"decision": "allow"}})):
        _create(harness, llm, hooks={"timeout_ms": 2000})
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    body = str(llm.requests[0])
    assert marker in body, body[:500]


def test_turn_start_payload_carries_messages(llm):
    harness = Harness()
    result, host = _run(harness, llm, {"turn_start": {"context": ""}, "tool_pre": {"decision": "allow"}})

    starts = [item for item in host.requests if item["params"]["point"] == "turn_start"]
    assert starts, host.requests
    payload = starts[0]["params"]["payload"]
    assert payload["input"] == "请执行"
    assert any(item["role"] == "human" for item in payload["messages"])
    assert starts[0]["id"] == "t1#h1"


# ============================== tool_pre / tool_post ==============================


def test_tool_pre_deny_skips_body_and_reports_reason(llm):
    harness = Harness()
    result, host = _run(
        harness,
        llm,
        {"turn_start": {"context": ""}, "tool_pre": {"decision": "deny", "reason": "危险命令"}},
    )

    assert CALLS == [], "deny 后工具本体不得执行"
    assert result["status"] == "finished"
    assert "危险命令" in str(llm.requests[-1])

    pre = [item for item in host.requests if item["params"]["point"] == "tool_pre"]
    assert pre and pre[0]["params"]["payload"]["tool_name"] == "probe_tool"
    assert pre[0]["params"]["payload"]["tool_use_id"] == "call_1"


def test_tool_pre_allow_executes_tool(llm):
    harness = Harness()
    result, _ = _run(harness, llm, {"turn_start": {"context": ""}, "tool_pre": {"decision": "allow"}})

    assert result["status"] == "finished"
    assert CALLS == ["x"]
    assert "tool-result:x" in str(llm.requests[-1])


def test_tool_post_context_is_appended(llm):
    harness = Harness()
    _run(
        harness,
        llm,
        {
            "turn_start": {"context": ""},
            "tool_pre": {"decision": "allow"},
            "tool_post": {"decision": "context", "context": "追加的背景补充"},
        },
    )

    body = str(llm.requests[-1])
    assert "追加的背景补充" in body
    assert "tool-result:x" in body, "context 只追加、不覆盖原结果"


# ============================== 降级 ==============================


def test_timeout_degrades_once_and_turn_finishes(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 100, "points": ["turn_start"]})
    with FakeHost(harness, silent):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    degraded = [
        event
        for event in harness.events("t1")
        if event["type"] == "CUSTOM" and event.get("name") == "hook_degraded"
    ]
    assert len(degraded) == 1, degraded
    assert degraded[0]["value"] == {"point": "turn_start", "reason": "timeout"}
    assert harness.result("t1")["status"] == "finished"


def test_degraded_point_does_not_block_turn(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000, "points": ["tool_pre"]})

    def _host_error(request):
        return {"id": request["id"], "ok": False, "error": {"code": "boom", "message": "宿主失败"}}

    with FakeHost(harness, _host_error):
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    degraded = [
        event for event in harness.events("t1") if event["type"] == "CUSTOM" and event.get("name") == "hook_degraded"
    ]
    assert [event["value"]["reason"] for event in degraded] == ["host_error"]


# ============================== 协议形状 ==============================


def test_request_line_shape_and_seq(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000})
    with FakeHost(harness, allow_all()) as host:
        harness.send("t1", "turn", session_code="s1", input="请执行")
        harness.wait()

    requests = host.hook_requests()
    assert requests
    first = requests[0]
    assert first["method"] == "hook"
    assert set(first["params"]) == {"session_code", "turn_id", "point", "payload"}
    assert first["params"]["session_code"] == "s1"
    assert all(item["id"].startswith("t1#h") for item in requests)
    assert len({item["id"] for item in requests}) == len(requests)


def test_invalid_hooks_config_is_rejected(llm):
    harness = Harness()
    harness.send_obj(
        "c1",
        "create",
        {
            "session_code": "s1",
            "agent": {"agent_code": "agent-hook"},
            "llm": {"base_url": llm.base_url, "model": "fake-model"},
            "hooks": {"timeout_ms": 0},
        },
    )
    assert harness.error("c1")["code"] == "hook.value_invalid"
