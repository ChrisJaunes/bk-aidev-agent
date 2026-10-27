# -*- coding: utf-8 -*-
"""T-8 / REQ-10：生命周期到期清理、引用延期与失败隔离。"""

from __future__ import annotations

import logging

from aidev_agent.core.packages.context_engine import EntryStatus

LOG_PREFIX = "[ContextOffloading]"


def test_end_turn_evicts_turn_level_entries(make_store):
    store = make_store()
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    user_input = store.write(scope="s1", category="Current User Input", payload={"n": 2}, source="user_request")
    history = store.write(scope="s1", category="Conversation History", payload={"n": 3}, source="chat_summary")

    report = store.end_turn(scope="s1")

    assert (report.expired, report.evicted) == (2, 2)
    assert trace.id not in store._entries["s1"]
    assert user_input.id not in store._entries["s1"]
    assert store._entries["s1"][history.id].status is EntryStatus.ACTIVE


def test_end_request_evicts_request_level_entries(make_store):
    store = make_store()
    governance = store.write(scope="s1", category="Environment & Governance", payload={"n": 1}, source="policy_engine")

    report = store.end_request(scope="s1")

    assert (report.expired, report.evicted) == (1, 1)
    assert governance.id not in store._entries["s1"]


def test_end_session_offloads_history_and_keeps_it_loadable(make_store):
    store = make_store()
    payload = {"turns": [{"role": "user", "content": "继续"}]}
    history = store.write(scope="s1", category="Conversation History", payload=payload, source="chat_summary")

    report = store.end_session(scope="s1")

    assert (report.expired, report.offloaded, report.evicted) == (1, 1, 0)
    assert store._entries["s1"][history.id].status is EntryStatus.OFFLOADED
    loaded = store.load(scope="s1").entries
    assert [item.payload for item in loaded] == [payload]


def test_end_task_offloads_working_state(make_store):
    store = make_store()
    state = store.write(scope="s1", category="Working State", payload={"goal": "完成 T-8"}, source="state_store")

    report = store.end_task(scope="s1")

    assert (report.expired, report.offloaded) == (1, 1)
    assert store._entries["s1"][state.id].status is EntryStatus.OFFLOADED


def test_end_events_never_expire_long_lived_lifecycles(make_store):
    store = make_store()
    kept_ids = {
        store.write(scope="s1", category="System Instructions", payload={"n": 1}, source="sdk").id,
        store.write(scope="s1", category="Agent Definition", payload={"n": 2}, source="prompt").id,
        store.write(scope="s1", category="User Context", payload={"n": 3}, source="user_system").id,
    }

    for end_event in (store.end_turn, store.end_request, store.end_session, store.end_task):
        report = end_event(scope="s1")
        assert report.expired == 0

    assert set(store._entries["s1"]) == kept_ids
    assert store.stats().hot_entries == 3


def test_referenced_entry_is_deferred_until_release(make_store):
    store = make_store()
    state = store.write(scope="s1", category="Working State", payload={"goal": "长任务"}, source="state_store")
    store.retain(scope="s1", entry_ids=[state.id])

    deferred = store.end_task(scope="s1")

    assert (deferred.expired, deferred.deferred, deferred.offloaded) == (1, 1, 0)
    assert store._entries["s1"][state.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][state.id].payload == {"goal": "长任务"}

    store.release(scope="s1", entry_ids=[state.id])
    assert store._entries["s1"][state.id].ref_count == 0

    after_release = store.end_task(scope="s1")

    assert (after_release.offloaded, after_release.deferred) == (1, 0)
    assert store._entries["s1"][state.id].status is EntryStatus.OFFLOADED


def test_release_never_goes_below_zero(store):
    entry = store.write(scope="s1", category="Working State", payload={"n": 1}, source="state_store")

    store.release(scope="s1", entry_ids=[entry.id])

    assert store._entries["s1"][entry.id].ref_count == 0


def test_single_entry_failure_does_not_block_others(make_store, toggle_cold):
    store = make_store(cold=toggle_cold)
    broken = store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    healthy = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")
    toggle_cold.fail_ids = {broken.id}

    report = store.end_session(scope="s1")

    assert (report.failed, report.offloaded) == (1, 1)
    assert store._entries["s1"][broken.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][healthy.id].status is EntryStatus.OFFLOADED


def test_consecutive_failures_warn_once_and_reset_after_success(make_store, toggle_cold, caplog):
    store = make_store(cold=toggle_cold)
    failing = [
        store.write(scope="s1", category="Conversation History", payload={"n": index}, source="chat_summary")
        for index in range(3)
    ]
    toggle_cold.fail_ids = {entry.id for entry in failing}

    with caplog.at_level(logging.ERROR):
        first = store.end_session(scope="s1")
        assert first.failed == 3

        errors_after_three = [record for record in caplog.records if record.levelno == logging.ERROR]
        assert len(errors_after_three) == 1
        assert LOG_PREFIX in errors_after_three[0].getMessage()

        # 成功一次后计数清零（此前失败的 3 条在本轮被重新处理并卸载成功）
        toggle_cold.fail_ids = set()
        store.write(scope="s1", category="Conversation History", payload={"n": 9}, source="chat_summary")
        recovered = store.end_session(scope="s1")
        assert recovered.failed == 0
        assert recovered.offloaded == 4
        assert all(store._entries["s1"][entry.id].status is EntryStatus.OFFLOADED for entry in failing)

        # 再次失败两次仍未达到阈值，不再产生 error 告警
        again = [
            store.write(scope="s1", category="Conversation History", payload={"n": 10 + index}, source="chat_summary")
            for index in range(2)
        ]
        toggle_cold.fail_ids = {entry.id for entry in again}
        assert store.end_session(scope="s1").failed == 2

    errors_total = [
        record for record in caplog.records if record.levelno == logging.ERROR and LOG_PREFIX in record.getMessage()
    ]
    assert len(errors_total) == 1


def test_end_events_only_touch_given_scope(make_store):
    store = make_store()
    store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    other = store.write(scope="s2", category="Execution Trace", payload={"n": 2}, source="tool_call")

    report = store.end_turn(scope="s1")

    assert report.expired == 1
    assert other.id in store._entries["s2"]
