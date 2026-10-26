# -*- coding: utf-8 -*-
"""T-8：并发、串行与取消（REQ-10 / ACC-9）。"""

import threading
import time
import unittest.mock as mock

import pytest
from ag_ui.core import EventType
from aidev_agent.headless.cli import assert_in_memory_message_handler
from aidev_agent.utils.event import RunId

from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness

SLOW_REPLY = "慢慢说的话就会比较久" * 2


@pytest.fixture
def slow_llm():
    with FakeLLMServer([SLOW_REPLY], chunk_delay=0.05) as server:
        yield server


def _create(harness: Harness, llm: FakeLLMServer, session_code: str, request_id: str):
    harness.send_obj(
        request_id,
        "create",
        {
            "session_code": session_code,
            "agent": {"agent_code": "agent-hook"},
            "llm": {"base_url": llm.base_url, "model": "fake-model"},
        },
    )
    harness.result(request_id)


def test_same_session_turns_are_serialised(slow_llm):
    harness = Harness()
    _create(harness, slow_llm, "s1", "c1")

    harness.send("t1", "turn", session_code="s1", input="第一轮")
    harness.send("t2", "turn", session_code="s1", input="第二轮")
    harness.wait(60)

    lines = harness.lines()
    first_finished = next(
        index
        for index, line in enumerate(lines)
        if line.get("id") == "t1" and line.get("event", {}).get("type") == EventType.RUN_FINISHED.value
    )
    second_event_indexes = [
        index for index, line in enumerate(lines) if line.get("id") == "t2" and line.get("type") == "event"
    ]
    # 第二个 turn 的任何事件都必须晚于第一个 turn 的终止事件
    assert second_event_indexes, "第二个 turn 应有事件"
    assert min(second_event_indexes) > first_finished

    assert harness.result("t1")["status"] == "finished"
    assert harness.result("t2")["status"] == "finished"
    assert slow_llm.max_concurrency == 1


def test_different_sessions_run_concurrently(slow_llm):
    harness = Harness()
    _create(harness, slow_llm, "s1", "c1")
    _create(harness, slow_llm, "s2", "c2")

    started = time.monotonic()
    harness.send("t1", "turn", session_code="s1", input="一")
    harness.send("t2", "turn", session_code="s2", input="二")
    harness.wait(60)
    elapsed = time.monotonic() - started

    assert harness.result("t1")["status"] == "finished"
    assert harness.result("t2")["status"] == "finished"
    # 两个会话可同时处于执行中（fake 端点观测到并发请求）
    assert slow_llm.max_concurrency >= 2, f"未观测到跨会话并发，耗时 {elapsed:.2f}s"


def test_cancel_ends_the_turn_with_cancel_terminal_events(slow_llm):
    harness = Harness()
    _create(harness, slow_llm, "s1", "c1")
    harness.send("t1", "turn", session_code="s1", input="很长的输入")

    session = harness.dispatcher.registry.get("s1")
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not any(
        event["type"] == EventType.RUN_STARTED.value for event in harness.events("t1")
    ):
        time.sleep(0.02)

    started = time.monotonic()
    harness.send("x1", "cancel", session_code="s1")
    assert harness.result("x1") == {"session_code": "s1", "cancelled": True}
    harness.wait(30)
    assert time.monotonic() - started < 10, "cancel 必须在有界时间内收敛"

    result = harness.result("t1")
    assert result["status"] == "cancelled"
    types = harness.event_types("t1")
    assert types[-2:] == [EventType.RUN_ERROR.value, EventType.RUN_FINISHED.value]

    events = harness.events("t1")
    assert events[-2]["message"] == RunId.CANCELLED_MESSAGE
    assert events[-1]["runId"] == RunId.CANCELLED
    assert session.closed is False


def test_cancel_without_active_turn_is_noop(slow_llm):
    harness = Harness()
    _create(harness, slow_llm, "s1", "c1")
    harness.send("x1", "cancel", session_code="s1")
    assert harness.result("x1") == {"session_code": "s1", "cancelled": True}


def test_non_in_memory_handler_fails_startup_explicitly():
    class _FakeRabbit:
        pass

    with mock.patch(
        "aidev_agent.services.messages_handler.factory.message_handler_factory",
        return_value=_FakeRabbit(),
    ):
        with pytest.raises(SystemExit) as exc_info:
            assert_in_memory_message_handler()
    assert "_FakeRabbit" in str(exc_info.value)
