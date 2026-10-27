# -*- coding: utf-8 -*-
"""T-11 / REQ-13：十类内置类别的样例数据全链路（写入 → 加载 → 卸载 → 回捞 → 到期清理）。"""

from __future__ import annotations

from collections import Counter

from aidev_agent.core.packages.context_engine import (
    CategorySpec,
    EntryFilter,
    EntryStatus,
    Lifecycle,
)
from aidev_agent.core.packages.context_engine.models import ContextEntry
from aidev_agent.core.packages.context_engine.registry import builtin_category_names


def _by_id(entries) -> dict[str, ContextEntry]:
    return {entry.id: entry for entry in entries}


def test_builtin_samples_cover_ten_categories_and_sub_contexts(builtin_samples):
    categories = Counter(sample["category"] for sample in builtin_samples)

    assert set(categories) == set(builtin_category_names())
    working_state_labels = {sample["labels"] for sample in builtin_samples if sample["category"] == "Working State"}
    governance_labels = {
        sample["labels"] for sample in builtin_samples if sample["category"] == "Environment & Governance"
    }
    assert len(working_state_labels) >= 2
    assert len(governance_labels) >= 2


def test_builtin_categories_full_cycle(make_store, clock, builtin_samples):
    store = make_store(idle_seconds=60)

    written = [store.write(scope="s1", **sample) for sample in builtin_samples]

    # 1) 写入后按过滤器加载，内容与提交一致（AC-1）
    loaded = store.load(scope="s1").entries
    assert len(loaded) == len(written)
    written_by_id = _by_id(written)
    for entry in loaded:
        origin = written_by_id[entry.id]
        assert entry.payload == origin.payload
        assert entry.category == origin.category
        assert entry.lifecycle == origin.lifecycle
        assert entry.source == origin.source
        assert entry.labels == origin.labels
        assert entry.status is EntryStatus.ACTIVE

    turn_and_session = store.load(
        scope="s1", entry_filter=EntryFilter(lifecycles=(Lifecycle.TURN, Lifecycle.SESSION))
    ).entries
    assert {entry.category for entry in turn_and_session} == {
        "Execution Trace",
        "Current User Input",
        "Conversation History",
    }

    # 2) 闲置卸载：不可卸载类别保持热层（AC-5 / AC-11）
    clock.advance(120)
    offload_report = store.sweep()
    assert offload_report.offloaded == len(written) - 1
    stats = store.stats()
    assert (stats.hot_entries, stats.offloaded_entries) == (1, len(written) - 1)
    instruction_ids = [entry.id for entry in written if entry.category == "System Instructions"]
    for entry_id in instruction_ids:
        assert store._entries["s1"][entry_id].status is EntryStatus.ACTIVE

    # 3) 回捞：加载即自动还原，调用方无需额外操作（AC-5）
    clock.advance(30)
    restored = store.load(scope="s1")
    assert restored.degraded is False
    assert restored.skipped_ids == ()
    assert len(restored.entries) == len(written)
    assert store.stats().offloaded_entries == 0
    for entry in restored.entries:
        assert entry.status is EntryStatus.ACTIVE
        assert entry.payload == written_by_id[entry.id].payload

    # 4) 到期清理：高优先级直接淘汰、中优先级卸载保留（AC-10）
    turn_report = store.end_turn(scope="s1")
    assert turn_report.evicted == 2  # Execution Trace(100) + Current User Input(90)
    request_report = store.end_request(scope="s1")
    assert request_report.evicted == 2  # Environment & Governance(80)
    session_report = store.end_session(scope="s1")
    assert session_report.offloaded == 1  # Conversation History(60)
    task_report = store.end_task(scope="s1")
    assert task_report.offloaded == 2  # Working State(70)

    remaining = store.load(scope="s1").entries
    remaining_categories = Counter(entry.category for entry in remaining)
    # 到期的轮次级/请求级条目被淘汰；其余类别或留在热层、或卸载后再次回捞
    assert remaining_categories == Counter(
        {
            "System Instructions": 1,
            "Agent Definition": 1,
            "Tool Definitions": 1,
            "Skill Context": 1,
            "User Context": 1,
            "Conversation History": 1,
            "Working State": 2,
        }
    )


def test_new_category_runs_same_full_cycle(make_store, clock):
    """AC-7：注册新类别后跑通同一条链路，且核心代码零修改。"""

    store = make_store(idle_seconds=60)
    store.register_category(
        CategorySpec(
            name="Memory Context",
            default_lifecycle=Lifecycle.SESSION,
            sources=("memory_store",),
            eviction_priority=65,
        )
    )
    payload = {"fact": "用户偏好中文回复", "confidence": 0.9}

    entry = store.write(scope="s1", category="Memory Context", payload=payload, source="memory_store")
    assert store.load(scope="s1").entries[0].payload == payload

    clock.advance(120)
    assert store.sweep().offloaded == 1
    assert store._entries["s1"][entry.id].status is EntryStatus.OFFLOADED

    restored = store.load(scope="s1").entries
    assert restored[0].payload == payload
    assert restored[0].status is EntryStatus.ACTIVE

    report = store.end_session(scope="s1")
    assert (report.expired, report.offloaded) == (1, 1)
    assert store.load(scope="s1").entries[0].payload == payload
