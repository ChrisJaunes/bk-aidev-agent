# -*- coding: utf-8 -*-
"""T-3 / T-6 / T-7：冷层协议、闲置卸载与回捞、容量淘汰与永不淘汰约束。"""

from __future__ import annotations

import json
import logging
import threading

import pytest
from aidev_agent.core.packages.context_engine import (
    KIND_BACKUP,
    KIND_OFFLOADED,
    ColdBackend,
    EntryStatus,
    InMemoryColdBackend,
)

LOG_PREFIX = "[ContextOffloading]"


# --------------------------------------------------------------- 冷层协议（T-3）


def test_cold_backend_put_get_count_and_overwrite():
    cold = InMemoryColdBackend()

    cold.put("s1", "e1", '{"a": 1}', KIND_BACKUP)
    cold.put("s1", "e2", '{"a": 2}', KIND_OFFLOADED)
    cold.put("s1", "e1", '{"a": 3}', KIND_OFFLOADED)

    assert cold.get("s1", "e1") == (KIND_OFFLOADED, '{"a": 3}')
    assert cold.count() == 2
    assert cold.count(KIND_BACKUP) == 0
    assert cold.count(KIND_OFFLOADED) == 2

    cold.delete("s1", "e1")
    assert cold.get("s1", "e1") is None
    assert cold.count() == 1


def test_cold_backend_is_scope_isolated():
    cold = InMemoryColdBackend()
    cold.put("s1", "e1", "a", KIND_BACKUP)
    cold.put("s2", "e1", "b", KIND_BACKUP)

    assert cold.get("s1", "e1") == (KIND_BACKUP, "a")
    assert cold.get("s2", "e1") == (KIND_BACKUP, "b")


def test_cold_backend_satisfies_protocol():
    assert isinstance(InMemoryColdBackend(), ColdBackend)


def test_cold_backend_handles_concurrent_put_get():
    cold = InMemoryColdBackend()
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            for round_index in range(50):
                key = f"e{index}-{round_index % 5}"
                cold.put("s1", key, f"{index}-{round_index}", KIND_BACKUP)
                cold.get("s1", key)
                cold.count(KIND_BACKUP)
        except BaseException as exc:  # noqa: BLE001 - 测试需暴露任何异常
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_cold_backend_failure_injection_raises(toggle_cold):
    toggle_cold.fail_all = True

    with pytest.raises(RuntimeError):
        toggle_cold.put("s1", "e1", "blob", KIND_BACKUP)
    with pytest.raises(RuntimeError):
        toggle_cold.get("s1", "e1")
    with pytest.raises(RuntimeError):
        toggle_cold.count()


# ------------------------------------------------------- 闲置卸载与回捞（T-6）


def test_idle_entries_are_offloaded(make_store, clock):
    store = make_store(idle_seconds=60)
    entry = store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    clock.advance(60)

    report = store.sweep()

    assert report.offloaded == 1
    hot_entry = store._entries["s1"][entry.id]
    assert hot_entry.status is EntryStatus.OFFLOADED
    assert hot_entry.payload is None
    stats = store.stats()
    assert (stats.hot_entries, stats.offloaded_entries) == (0, 1)
    assert store._cold.count(KIND_OFFLOADED) == 1


def test_idle_offload_respects_threshold_boundary(make_store, clock):
    store = make_store(idle_seconds=60)
    store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    clock.advance(59)

    assert store.sweep().offloaded == 0

    clock.advance(1)
    assert store.sweep().offloaded == 1


def test_load_rehydrates_offloaded_entry(make_store, clock):
    store = make_store(idle_seconds=60)
    payload = {"turns": [{"role": "user", "content": "你好"}]}
    entry = store.write(scope="s1", category="Conversation History", payload=payload, source="chat_summary")
    clock.advance(120)
    store.sweep()
    clock.advance(5)

    result = store.load(scope="s1")

    assert result.degraded is False
    assert result.skipped_ids == ()
    loaded = result.entries[0]
    assert loaded.id == entry.id
    assert loaded.payload == payload
    assert loaded.status is EntryStatus.ACTIVE
    assert loaded.last_accessed_at == clock.now
    assert store.stats().rehydrate_count == 1
    assert store._cold.count(KIND_OFFLOADED) == 0
    assert store._cold.count(KIND_BACKUP) == 1


def test_degraded_load_when_cold_read_fails(make_store, clock, toggle_cold):
    store = make_store(cold=toggle_cold, idle_seconds=60)
    offloaded = store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    clock.advance(120)
    store.sweep()
    hot = store.write(scope="s1", category="Execution Trace", payload={"n": 2}, source="tool_call")
    toggle_cold.fail_reads = True

    result = store.load(scope="s1")

    assert result.degraded is True
    assert result.skipped_ids == (offloaded.id,)
    assert [item.id for item in result.entries] == [hot.id]


def test_write_and_update_survive_cold_write_failure(make_store, toggle_cold):
    store = make_store(cold=toggle_cold)
    toggle_cold.fail_writes = True

    entry = store.write(scope="s1", category="Agent Definition", payload={"v": 1}, source="prompt")
    updated = store.update(scope="s1", entry_id=entry.id, version=1, payload={"v": 2})

    assert updated.version == 2
    assert store.load(scope="s1").entries[0].payload == {"v": 2}
    assert toggle_cold.inner.count() == 0


def test_offload_is_skipped_when_cold_unavailable(make_store, clock, toggle_cold):
    store = make_store(cold=toggle_cold, idle_seconds=60)
    entry = store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    clock.advance(120)
    toggle_cold.fail_all = True

    report = store.sweep()

    assert report.failed == 1
    assert store._entries["s1"][entry.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][entry.id].payload == {"n": 1}


def test_offload_uses_hot_payload_when_cold_backup_is_stale(make_store, clock, toggle_cold):
    """F-1：冷层写失败期间提交的更新，不得在下一次卸载-回捞后回滚为旧版本。"""

    store = make_store(cold=toggle_cold, idle_seconds=60)
    entry = store.write(scope="s1", category="Conversation History", payload={"v": 1}, source="chat_summary")
    assert json.loads(toggle_cold.inner.get("s1", entry.id)[1]) == {"v": 1}

    # 冷层写失败下更新成功：热层已是最新版本，冷层备份仍是旧版本
    toggle_cold.fail_writes = True
    updated = store.update(scope="s1", entry_id=entry.id, version=1, payload={"v": 2})
    assert updated.version == 2
    assert json.loads(toggle_cold.inner.get("s1", entry.id)[1]) == {"v": 1}

    # 冷层恢复后越过闲置阈值卸载：offloaded 记录必须是热层的最新 payload
    toggle_cold.fail_writes = False
    clock.advance(120)
    report = store.sweep()
    assert (report.offloaded, report.failed) == (1, 0)

    offloaded_kind, offloaded_blob = toggle_cold.inner.get("s1", entry.id)
    assert offloaded_kind == KIND_OFFLOADED
    assert json.loads(offloaded_blob) == {"v": 2}

    # 回捞返回最新 payload，且无降级信号；冷层翻回的备份同为最新 payload
    result = store.load(scope="s1")
    assert result.degraded is False
    assert result.skipped_ids == ()
    loaded = result.entries[0]
    assert loaded.payload == {"v": 2}
    assert loaded.status is EntryStatus.ACTIVE
    assert loaded.version == 2

    backup_kind, backup_blob = toggle_cold.inner.get("s1", entry.id)
    assert backup_kind == KIND_BACKUP
    assert json.loads(backup_blob) == {"v": 2}


# ----------------------------------------------------------- 容量淘汰（T-7）


def test_eviction_order_follows_priority(make_store):
    store = make_store(hot_capacity=2, idle_seconds=10**6, sweep_interval=600)
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    history = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")
    agent = store.write(scope="s1", category="Agent Definition", payload={"n": 3}, source="prompt")

    report = store.sweep()

    assert report.offloaded == 1
    assert store._entries["s1"][trace.id].status is EntryStatus.OFFLOADED
    assert store._entries["s1"][history.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][agent.id].status is EntryStatus.ACTIVE
    assert store.stats().hot_entries == 2


def test_eviction_uses_lru_within_same_priority(make_store, clock):
    store = make_store(hot_capacity=1, idle_seconds=10**6, sweep_interval=600)
    older = store.write(scope="s1", category="Conversation History", payload={"n": 1}, source="chat_summary")
    clock.advance(30)
    newer = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")

    store.sweep()

    assert store._entries["s1"][older.id].status is EntryStatus.OFFLOADED
    assert store._entries["s1"][newer.id].status is EntryStatus.ACTIVE


def test_eviction_never_touches_system_instructions(make_store, caplog):
    store = make_store(hot_capacity=1, idle_seconds=10**6, sweep_interval=600)
    instructions = [
        store.write(scope="s1", category="System Instructions", payload={"i": index}, source="sdk")
        for index in range(3)
    ]
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    trace2 = store.write(scope="s1", category="Execution Trace", payload={"n": 2}, source="tool_call")

    with caplog.at_level(logging.WARNING):
        report = store.sweep()

    assert report.offloaded == 2
    for entry in instructions:
        assert store._entries["s1"][entry.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][trace.id].status is EntryStatus.OFFLOADED
    assert store._entries["s1"][trace2.id].status is EntryStatus.OFFLOADED
    assert store.stats().hot_entries == 3  # 允许暂时超限而不是淘汰不可卸载类别
    assert any("无法完全卸载" in record.getMessage() for record in caplog.records)

    loaded_ids = {item.id for item in store.load(scope="s1").entries}
    assert {entry.id for entry in instructions}.issubset(loaded_ids)


def test_eviction_reclaims_cold_when_over_capacity(make_store):
    store = make_store(hot_capacity=1, cold_capacity=1, idle_seconds=10**6, sweep_interval=600)
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    history = store.write(scope="s1", category="Conversation History", payload={"n": 2}, source="chat_summary")
    agent = store.write(scope="s1", category="Agent Definition", payload={"n": 3}, source="prompt")

    report = store.sweep()

    assert (report.offloaded, report.evicted) == (2, 1)
    stats = store.stats()
    assert (stats.hot_entries, stats.offloaded_entries, stats.eviction_count) == (1, 1, 1)
    assert trace.id not in store._entries["s1"]
    assert store._entries["s1"][agent.id].status is EntryStatus.ACTIVE
    assert store._entries["s1"][history.id].status is EntryStatus.OFFLOADED
    assert store.load(scope="s1").degraded is False


def test_offload_and_rehydrate_are_logged(make_store, clock, caplog):
    store = make_store(idle_seconds=60)
    entry = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    clock.advance(120)

    with caplog.at_level(logging.INFO):
        store.sweep()
        store.load(scope="s1")

    messages = [record.getMessage() for record in caplog.records if LOG_PREFIX in record.getMessage()]
    assert any("卸载" in message and entry.id in message and "Execution Trace" in message for message in messages)
    assert any("回捞" in message and entry.id in message and "Execution Trace" in message for message in messages)
