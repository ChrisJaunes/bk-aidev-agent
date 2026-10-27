# -*- coding: utf-8 -*-
"""T-9 / REQ-14：只读统计、token 估算与惰性维护节流、日志覆盖。"""

from __future__ import annotations

import json
import logging

LOG_PREFIX = "[ContextOffloading]"


def test_stats_exposes_required_fields(make_store, clock):
    store = make_store(idle_seconds=60)
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    clock.advance(120)
    store.sweep()
    history = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")

    stats = store.stats()

    assert stats.hot_entries == 1
    assert stats.offloaded_entries == 1
    assert stats.backup_entries == 1
    assert stats.entries_by_category == {"Execution Trace": 1, "Conversation History": 1}
    assert stats.offload_count == 1
    assert stats.rehydrate_count == 0
    assert stats.eviction_count == 0
    assert stats.total_token_estimate > 0
    assert trace.id != history.id


def test_stats_is_read_only(make_store, clock, toggle_cold):
    store = make_store(cold=toggle_cold, idle_seconds=60)
    store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    active = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")
    clock.advance(120)
    store.sweep()

    before_entry = store._entries["s1"][active.id]
    before_state = (
        before_entry.status,
        before_entry.payload,
        before_entry.last_accessed_at,
        before_entry.version,
        toggle_cold.inner.count(),
    )
    first = store.stats()
    second = store.stats()

    after_entry = store._entries["s1"][active.id]
    assert first == second
    assert (
        after_entry.status,
        after_entry.payload,
        after_entry.last_accessed_at,
        after_entry.version,
        toggle_cold.inner.count(),
    ) == before_state
    assert store.stats().rehydrate_count == 0


def test_token_estimate_is_computed_once_and_summed(store):
    payload = {"text": "x" * 200}
    entry = store.write(scope="s1", category="Agent Definition", payload=payload, source="prompt")
    expected = max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)

    assert entry.token_estimate == expected
    assert store.stats().total_token_estimate == expected


def test_category_token_estimator_overrides_default(store):
    from aidev_agent.core.packages.context_engine import CategorySpec, Lifecycle

    store.register_category(
        CategorySpec(
            name="Tiny Context",
            default_lifecycle=Lifecycle.TURN,
            sources=("unit-test",),
            eviction_priority=10,
            token_estimator=lambda blob: 42,
        )
    )

    entry = store.write(scope="s1", category="Tiny Context", payload={"a": "b"}, source="unit-test")

    assert entry.token_estimate == 42


def test_maintenance_is_throttled_by_sweep_interval(make_store, clock, monkeypatch):
    store = make_store(sweep_interval=60)
    calls: list[int] = []
    original_sweep = store.sweep

    def spy(**kwargs):
        calls.append(1)
        return original_sweep(**kwargs)

    monkeypatch.setattr(store, "sweep", spy)

    store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    store.write(scope="s1", category="Execution Trace", payload={"n": 2}, source="tool_call")
    assert len(calls) == 1

    clock.advance(60)
    store.write(scope="s1", category="Execution Trace", payload={"n": 3}, source="tool_call")
    assert len(calls) == 2


def test_logs_cover_offload_rehydrate_evict_and_degrade(make_store, clock, toggle_cold, caplog):
    store = make_store(cold=toggle_cold, idle_seconds=60)
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    clock.advance(120)

    with caplog.at_level(logging.INFO):
        store.sweep()  # 卸载
        store.load(scope="s1")  # 回捞
        store.end_turn(scope="s1")  # 到期清理 + 淘汰

        history = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")
        clock.advance(120)
        store.sweep()
        toggle_cold.fail_reads = True
        result = store.load(scope="s1")  # 降级

    assert result.degraded is True
    messages = [record.getMessage() for record in caplog.records if LOG_PREFIX in record.getMessage()]

    assert any("卸载" in message and trace.id in message and "Execution Trace" in message for message in messages)
    assert any("回捞" in message and trace.id in message and "Execution Trace" in message for message in messages)
    assert any("淘汰" in message and trace.id in message and "Execution Trace" in message for message in messages)
    assert any("到期淘汰" in message and trace.id in message for message in messages)
    assert any(
        "降级" in message and history.id in message and "Conversation History" in message for message in messages
    )
