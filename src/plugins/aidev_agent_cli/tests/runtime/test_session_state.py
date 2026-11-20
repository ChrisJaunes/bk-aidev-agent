# -*- coding: utf-8 -*-
"""跨轮状态的分工：sdk 只合并本轮、runtime 决定提交；失败 / 取消 / 强杀整体回滚（含 ``turn_count``）。"""

from __future__ import annotations

from dataclasses import asdict
from types import SimpleNamespace

import pytest

from aidev_agent_cli.runtime.runs import _committed_state
from aidev_agent_cli.utils.runs import SessionState
from tests._sdk import make_run

PREVIOUS = SessionState(
    records=[{"role": "user", "content": "old", "message_id": "m0", "turn_id": "t0"}],
    interrupts=[],
    turn_count=2,
    hook_memo={},
    hook_sequence=5,
)


def _result(outcome_status: str, *, status: str = "finished", forced: bool = False, state=None):
    value = {"outcome": {"status": outcome_status}, "state": asdict(state or PREVIOUS)}
    return SimpleNamespace(status=status, forced=forced, value=value)


@pytest.mark.parametrize("outcome", ["finished", "interrupted"])
def test_runtime_commits_settled_turns(outcome):
    assert _committed_state("start", _result(outcome)) == PREVIOUS


@pytest.mark.parametrize(
    "result",
    [
        _result("failed"),
        _result("cancelled"),
        _result("finished", status="cancelled"),
        _result("finished", status="failed"),
        _result("finished", forced=True),
    ],
)
def test_runtime_rolls_back_failed_cancelled_and_forced_turns(result):
    assert _committed_state("start", result) is None


def test_queries_never_commit_state():
    assert _committed_state("describe_agent_config", _result("finished")) is None


class _Agent:
    event_handler = None

    def __init__(self, run):
        self.run = run

    def execute(self, _kwargs):
        self.run.writer({"type": "TEXT_MESSAGE_START", "messageId": "a", "role": "assistant"})
        self.run.writer({"type": "TEXT_MESSAGE_CONTENT", "messageId": "a", "delta": "new"})
        self.run.writer({"type": "TEXT_MESSAGE_END", "messageId": "a"})
        return iter(())

    def stop(self):
        return None


def _scripted(run):
    run.build_agent = lambda **_params: _Agent(run)
    run._event_handler = lambda: run.writer
    return run


def test_a_turn_merges_its_records_onto_the_previous_state():
    run = _scripted(make_run(session_code="s1", resources=("injected",), agent_code="a", state=PREVIOUS))
    try:
        outcome = run.turn(request_id="r1", input_text="hi")
        merged = run.state
    finally:
        run.close()

    assert outcome.status == "finished"
    assert merged.turn_count == 3
    assert [record["content"] for record in merged.records] == ["old", "hi", "new"]
    assert merged.hook_sequence == 5


def test_a_context_turn_leaves_the_state_untouched():
    run = _scripted(make_run(session_code="s1", resources=("injected",), agent_code="a", state=PREVIOUS))
    try:
        run.turn(request_id="r1", messages=[{"role": "user", "content": "x"}], context_mode=True)
        assert run.state == PREVIOUS
    finally:
        run.close()


def test_the_agent_snapshot_mirrors_the_agent_spec():
    """快照 agent 段与 AgentSpec 一一对应：只有 tool_factories ↔ tools 是跨进程形态的差异。"""
    from dataclasses import fields

    from aidev_agent_cli.runtime.models import AgentSnapshot
    from aidev_agent_cli.sdk.spec import AgentSpec

    snapshot = {item.name for item in fields(AgentSnapshot)} - {"tool_factories"}
    spec = {item.name for item in fields(AgentSpec)} - {"tools"}
    assert snapshot == spec


def test_the_worker_endpoint_is_the_turn_ports():
    """runtime.worker.WorkerContext 按结构直接充当 sdk 的 TurnPorts（外加本轮事件出口 emit）。"""
    from aidev_agent_cli.runtime.worker import WorkerContext
    from aidev_agent_cli.sdk.executor import WorkerPorts

    required = {name for name in dir(WorkerPorts) if not name.startswith("_")}
    assert required == {"building", "cancelled", "emit", "hook"}
    assert required <= set(dir(WorkerContext))
