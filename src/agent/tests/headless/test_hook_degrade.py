# -*- coding: utf-8 -*-
"""T-19：``HeadlessHookBus`` 超时、取消、fail-open 与 ``hook_degraded``（REQ-18 / REQ-19）。"""

from __future__ import annotations

import time

import pytest
from aidev_agent.headless.errors import HOOK_VALUE_INVALID, HeadlessError
from aidev_agent.headless.hooks import DEFAULT_TIMEOUT_MS, HookConfig

from tests.headless._fake_host import FakeHost, allow_all, silent
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
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def _degraded_events(harness: Harness, request_id: str) -> list[dict]:
    return [
        event
        for event in harness.events(request_id)
        if event["type"] == "CUSTOM" and event.get("name") == "hook_degraded"
    ]


# ============================== 配置解析 ==============================


def test_hook_config_defaults():
    config = HookConfig.parse(None)
    assert config.timeout_ms == DEFAULT_TIMEOUT_MS == 10000
    assert config.points() == frozenset({"session_start", "tool_pre", "tool_post"})


def test_hook_config_subset_and_clamp():
    config = HookConfig.parse({"timeout_ms": 50, "points": ["tool_pre"]})
    assert config.timeout_ms == 50
    assert config.enabled("tool_pre") is True
    assert config.enabled("tool_post") is False
    assert HookConfig.parse({"timeout_ms": 10**9}).timeout_ms == 3_600_000


@pytest.mark.parametrize(
    "raw",
    [
        {"timeout_ms": 0},
        {"timeout_ms": -1},
        {"timeout_ms": "1000"},
        {"timeout_ms": True},
        {"points": ["nope"]},
        {"points": "tool_pre"},
        [1, 2],
    ],
)
def test_hook_config_invalid_values(raw):
    with pytest.raises(HeadlessError) as excinfo:
        HookConfig.parse(raw)
    assert excinfo.value.code == HOOK_VALUE_INVALID


def test_create_with_invalid_hooks_returns_structured_error(llm):
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
    assert harness.error("c1")["code"] == HOOK_VALUE_INVALID


# ============================== 降级 ==============================


def test_timeout_degrades_once_and_turn_finishes(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 100})

    started = time.monotonic()
    with FakeHost(harness, silent):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()
    elapsed = time.monotonic() - started

    assert harness.result("t1")["status"] == "finished"
    degraded = _degraded_events(harness, "t1")
    assert len(degraded) == 1, degraded
    assert degraded[0]["value"] == {"point": "session_start", "reason": "timeout"}
    assert elapsed < 5, "降级不得引入可观延迟"


def test_host_error_degrades_and_turn_finishes(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000})

    def _host_error(request):
        return {"id": request["id"], "ok": False, "error": {"code": "host_boom", "message": "宿主内部错误"}}

    with FakeHost(harness, _host_error):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    assert [event["value"]["reason"] for event in _degraded_events(harness, "t1")] == ["host_error"]


def test_invalid_result_shape_degrades_and_turn_finishes(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000})

    def _bad_shape(request):
        return {"id": request["id"], "ok": True, "result": "not-an-object"}

    with FakeHost(harness, _bad_shape):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    assert [event["value"]["reason"] for event in _degraded_events(harness, "t1")] == ["invalid_shape"]


def test_session_start_context_must_be_string(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000})

    def _bad_context(request):
        return {"id": request["id"], "ok": True, "result": {"context": 123}}

    with FakeHost(harness, _bad_context):
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert harness.result("t1")["status"] == "finished"
    assert [event["value"]["reason"] for event in _degraded_events(harness, "t1")] == ["invalid_shape"]


# ============================== 取消 ==============================


def test_cancel_during_hook_wait_ends_before_timeout(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 5000})

    started = time.monotonic()
    with FakeHost(harness, silent) as host:
        harness.send("t1", "turn", session_code="s1", input="你好")
        deadline = time.monotonic() + 10
        while not host.hook_requests() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert host.hook_requests(), "本轮应停在 hook 等待上"
        harness.send("c2", "cancel", session_code="s1")
        harness.wait()
    elapsed = time.monotonic() - started

    assert harness.result("t1")["status"] == "cancelled"
    assert elapsed < 5, "取消必须在超时上限之前收敛"
    assert _degraded_events(harness, "t1") == [], "取消不写 hook_degraded"


# ============================== 关闭态零回归 ==============================


def test_close_state_has_no_request_lines_and_no_degraded(llm):
    harness = Harness()
    _create(harness, llm)
    harness.send("t1", "turn", session_code="s1", input="你好")
    harness.wait()

    lines = harness.lines()
    assert not [line for line in lines if line.get("type") == "request"], "关闭态不得出现 type=request"
    assert _degraded_events(harness, "t1") == []


def test_disabled_point_is_not_requested(llm):
    harness = Harness()
    _create(harness, llm, hooks={"timeout_ms": 2000, "points": ["tool_pre"]})

    with FakeHost(harness, allow_all()) as host:
        harness.send("t1", "turn", session_code="s1", input="你好")
        harness.wait()

    assert host.hook_requests() == [], "未启用的 session_start 不得产生请求行"
    assert harness.result("t1")["status"] == "finished"
