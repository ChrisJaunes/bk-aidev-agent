# -*- coding: utf-8 -*-
"""hook 协议与执行器契约测试。

覆盖 REQ-1 / REQ-6：点位与裁决取值、注入顺序、短路与覆盖、异常与超时降级、
未知裁决与缺载荷降级、同步与异步一致、结构化日志。

# AC12 test_exception_degrades_to_pass / test_timeout_degrades_to_pass
# AC13 test_handlers_run_in_injection_order / test_deny_short_circuits_following_handlers
# AC15 test_invalid_decision_degrades_to_pass / test_invalid_return_type_degrades_to_pass
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from aidev_agent.core.hook import (
    PASS,
    HookDecision,
    HookDecisionKind,
    HookIdentity,
    HookPoint,
    HookRunner,
    HookSet,
)
from aidev_agent.core.hook.contexts import ToolPostPayload, ToolPrePayload

IDENTITY = HookIdentity(agent_code="agent", session_code="session", run_id="run", turn=1)


def test_hook_points_are_fixed() -> None:
    """点位集合固定为 5 个，名称与拼写不得变更。"""
    assert [point.value for point in HookPoint] == [
        "session_start",
        "model_pre",
        "model_post",
        "tool_pre",
        "tool_post",
    ]


def test_hook_set_rejects_unknown_point() -> None:
    with pytest.raises(ValueError):
        HookSet({"unknown_point": [lambda context: None]})


def test_hook_set_rejects_non_callable() -> None:
    with pytest.raises(TypeError):
        HookSet({HookPoint.TOOL_PRE: ["not-callable"]})  # type: ignore[list-item]


def test_handlers_run_in_injection_order() -> None:
    """同点位处理器按注入顺序执行；replace 后写覆盖先写。"""
    calls: list[str] = []

    def first(context):
        calls.append("first")
        return HookDecision(kind=HookDecisionKind.REPLACE, payload={"value": "first"})

    def second(context):
        calls.append("second")
        return HookDecision(kind=HookDecisionKind.REPLACE, payload={"value": "second"})

    runner = HookRunner(HookSet({HookPoint.TOOL_PRE: [first, second]}))
    decision = runner.run_sync(HookPoint.TOOL_PRE, IDENTITY, ToolPrePayload(tool_name="t"))

    assert calls == ["first", "second"]
    assert decision.payload == {"value": "second"}


def test_deny_short_circuits_following_handlers() -> None:
    """AC13 首个 deny 短路，最终裁决为最严格者。"""
    calls: list[str] = []

    def deny(context):
        calls.append("deny")
        return HookDecision(kind=HookDecisionKind.DENY, reason="blocked")

    def later(context):
        calls.append("later")
        return None

    runner = HookRunner(HookSet({HookPoint.TOOL_PRE: [deny, later]}))
    decision = runner.run_sync(HookPoint.TOOL_PRE, IDENTITY, ToolPrePayload(tool_name="t"))

    assert decision.kind is HookDecisionKind.DENY
    assert calls == ["deny"]


def test_ask_short_circuits_on_tool_pre() -> None:
    def ask(context):
        return HookDecision(kind=HookDecisionKind.ASK, reason="confirm")

    runner = HookRunner(HookSet({HookPoint.TOOL_PRE: [ask]}))
    decision = runner.run_sync(HookPoint.TOOL_PRE, IDENTITY, ToolPrePayload(tool_name="t"))

    assert decision.kind is HookDecisionKind.ASK


def test_none_decision_equals_pass() -> None:
    runner = HookRunner(HookSet({HookPoint.MODEL_PRE: [lambda context: None]}))
    assert runner.run_sync(HookPoint.MODEL_PRE, IDENTITY, None) is PASS


def test_exception_degrades_to_pass(caplog: pytest.LogCaptureFixture) -> None:
    """AC12 处理器抛异常时跳过该处理器、记 warning，其余处理器继续。"""
    calls: list[str] = []

    def boom(context):
        calls.append("boom")
        raise RuntimeError("handler failed")

    def keep(context):
        calls.append("keep")
        return HookDecision(kind=HookDecisionKind.APPEND, payload="note")

    runner = HookRunner(HookSet({HookPoint.TOOL_POST: [boom, keep]}))
    with caplog.at_level(logging.WARNING):
        decision = runner.run_sync(HookPoint.TOOL_POST, IDENTITY, ToolPostPayload(tool_name="t"))

    assert calls == ["boom", "keep"]
    assert decision.payload == "note"
    assert "degraded" in caplog.text
    assert "exception" in caplog.text


def test_timeout_degrades_to_pass(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """AC12 单处理器超过 TIMEOUT_SECONDS 时按 pass 处理并记 warning。"""
    monkeypatch.setattr(HookRunner, "TIMEOUT_SECONDS", 0.05)

    def slow(context):
        time.sleep(0.5)
        return HookDecision(kind=HookDecisionKind.DENY, reason="too slow")

    runner = HookRunner(HookSet({HookPoint.TOOL_PRE: [slow]}))
    with caplog.at_level(logging.WARNING):
        decision = runner.run_sync(HookPoint.TOOL_PRE, IDENTITY, ToolPrePayload(tool_name="t"))

    assert decision is PASS
    assert "timeout" in caplog.text


@pytest.mark.parametrize(
    "decision",
    [
        HookDecision(kind="unknown"),  # type: ignore[arg-type]
        HookDecision(kind=HookDecisionKind.DENY, reason=""),
        HookDecision(kind=HookDecisionKind.REPLACE, payload=None),
        HookDecision(kind=HookDecisionKind.ASK, reason="not-allowed-here"),
    ],
)
def test_invalid_decision_degrades_to_pass(decision: HookDecision, caplog: pytest.LogCaptureFixture) -> None:
    """AC15 未知裁决 / 缺载荷 / 非本点位裁决一律按 pass 处理并记 warning。"""
    runner = HookRunner(HookSet({HookPoint.MODEL_PRE: [lambda context: decision]}))
    with caplog.at_level(logging.WARNING):
        result = runner.run_sync(HookPoint.MODEL_PRE, IDENTITY, None)

    assert result is PASS
    assert "degraded" in caplog.text


def test_invalid_return_type_degrades_to_pass(caplog: pytest.LogCaptureFixture) -> None:
    runner = HookRunner(HookSet({HookPoint.MODEL_PRE: [lambda context: "deny"]}))
    with caplog.at_level(logging.WARNING):
        assert runner.run_sync(HookPoint.MODEL_PRE, IDENTITY, None) is PASS
    assert "invalid_decision" in caplog.text


def test_sync_and_async_handlers_agree() -> None:
    """同步与异步处理器在同一输入下产出同一 HookDecision。"""

    def sync_handler(context):
        return HookDecision(kind=HookDecisionKind.REPLACE, payload={"args": {"q": "sync"}})

    async def async_handler(context):
        return HookDecision(kind=HookDecisionKind.REPLACE, payload={"args": {"q": "sync"}})

    payload = ToolPrePayload(tool_name="t", tool_call_id="call-1", arguments={"q": "raw"})
    sync_decision = HookRunner(HookSet({HookPoint.TOOL_PRE: [sync_handler]})).run_sync(
        HookPoint.TOOL_PRE, IDENTITY, payload
    )

    async def run() -> HookDecision:
        return await HookRunner(HookSet({HookPoint.TOOL_PRE: [async_handler]})).run_async(
            HookPoint.TOOL_PRE, IDENTITY, payload
        )

    async_decision = asyncio.run(run())

    assert sync_decision == async_decision


def test_each_execution_logs_structured_record(caplog: pytest.LogCaptureFixture) -> None:
    """每次处理器执行产生一条含 point / handler / decision / duration_ms 的日志。"""
    runner = HookRunner(HookSet({HookPoint.TOOL_PRE: [lambda context: None]}))
    with caplog.at_level(logging.INFO):
        runner.run_sync(HookPoint.TOOL_PRE, IDENTITY, ToolPrePayload(tool_name="t"))

    record = next(each for each in caplog.records if "[Hook] point=" in each.getMessage())
    message = record.getMessage()
    for field in ("point=tool_pre", "handler=", "decision=pass", "duration_ms="):
        assert field in message


def test_empty_hook_set_has_no_handlers() -> None:
    runner = HookRunner(HookSet())
    assert runner.has(HookPoint.SESSION_START) is False
    assert runner.run_sync(HookPoint.SESSION_START, IDENTITY, None) is PASS
