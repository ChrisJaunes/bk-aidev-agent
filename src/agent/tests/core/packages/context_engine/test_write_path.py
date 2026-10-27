# -*- coding: utf-8 -*-
"""T-4 / REQ-1、REQ-2、REQ-3：写入校验与更新并发冲突。"""

from __future__ import annotations

import json

import pytest
from aidev_agent.core.packages.context_engine import (
    EntryNotFoundError,
    EntryStatus,
    EntryValidationError,
    InMemoryColdBackend,
    Lifecycle,
    PayloadSerializationError,
    VersionConflictError,
)

UNSERIALIZABLE = object()


def _snapshot(store, cold):
    stats = store.stats()
    return (
        stats.hot_entries,
        stats.offloaded_entries,
        stats.total_token_estimate,
        cold.count(),
    )


def test_write_assigns_metadata(store, clock):
    entry = store.write(
        scope="s1",
        category="Execution Trace",
        payload={"tool": "search"},
        source="tool_call",
        labels=("step-1",),
    )

    assert entry.id and len(entry.id) == 32
    assert entry.version == 1
    assert entry.status is EntryStatus.ACTIVE
    assert entry.lifecycle is Lifecycle.TURN  # 类别默认生命周期
    assert entry.labels == ("step-1",)
    assert entry.created_at == clock.now == entry.updated_at == entry.last_accessed_at
    assert entry.token_estimate >= 1
    assert entry.ref_count == 0


def test_write_then_load_returns_original_payload(store):
    payload = {"turns": [{"role": "user", "content": "中文内容"}], "n": 3}
    entry = store.write(scope="s1", category="Conversation History", payload=payload, source="chat_summary")

    result = store.load(scope="s1")

    assert result.degraded is False
    assert len(result.entries) == 1
    loaded = result.entries[0]
    assert (loaded.id, loaded.category, loaded.source, loaded.lifecycle) == (
        entry.id,
        "Conversation History",
        "chat_summary",
        Lifecycle.SESSION,
    )
    assert loaded.payload == payload


def test_write_with_explicit_lifecycle(store):
    entry = store.write(
        scope="s1",
        category="User Context",
        payload={"user": "u-1"},
        source="user_system",
        lifecycle=Lifecycle.SESSION,
    )

    assert entry.lifecycle is Lifecycle.SESSION


def test_write_accepts_lifecycle_string(store):
    entry = store.write(scope="s1", category="User Context", payload={"a": 1}, source="s", lifecycle="turn")

    assert entry.lifecycle is Lifecycle.TURN


def test_write_keeps_token_estimate_of_serialized_payload(store):
    payload = {"text": "x" * 400}
    entry = store.write(scope="s1", category="Current User Input", payload=payload, source="user_request")

    assert entry.token_estimate == max(1, len(json.dumps(payload, ensure_ascii=False)) // 4)


@pytest.mark.parametrize(
    ("kwargs", "expected_error"),
    [
        ({"scope": ""}, EntryValidationError),
        ({"scope": "   "}, EntryValidationError),
        ({"scope": None}, EntryValidationError),
        ({"source": ""}, EntryValidationError),
        ({"source": None}, EntryValidationError),
        ({"category": None}, EntryValidationError),
        ({"category": ""}, EntryValidationError),
        ({"payload": None}, EntryValidationError),
        ({"lifecycle": "decade"}, EntryValidationError),
        ({"labels": "not-a-sequence"}, EntryValidationError),
        ({"labels": (1, 2)}, EntryValidationError),
        ({"sub_context": 1}, EntryValidationError),
    ],
)
def test_invalid_write_rejected_without_side_effect(make_store, kwargs, expected_error):
    cold = InMemoryColdBackend()
    store = make_store(cold=cold)
    params = {
        "scope": "s1",
        "category": "Execution Trace",
        "payload": {"tool": "search"},
        "source": "tool_call",
    }
    params.update(kwargs)

    with pytest.raises(expected_error):
        store.write(**params)

    assert _snapshot(store, cold) == (0, 0, 0, 0)
    assert store.load(scope="s1").entries == ()


def test_unserializable_payload_rejected_without_side_effect(make_store):
    cold = InMemoryColdBackend()
    store = make_store(cold=cold)

    with pytest.raises(PayloadSerializationError):
        store.write(scope="s1", category="Execution Trace", payload={"bad": UNSERIALIZABLE}, source="tool_call")

    assert _snapshot(store, cold) == (0, 0, 0, 0)


def test_update_increments_version_and_refreshes_timestamps(store, clock):
    entry = store.write(scope="s1", category="Agent Definition", payload={"v": 1}, source="prompt")
    clock.advance(30)

    updated = store.update(scope="s1", entry_id=entry.id, version=entry.version, payload={"v": 2})

    assert updated.version == entry.version + 1
    assert updated.updated_at > entry.updated_at
    assert updated.last_accessed_at == clock.now
    assert store.load(scope="s1").entries[0].payload == {"v": 2}


def test_update_recomputes_token_estimate(store):
    entry = store.write(scope="s1", category="Agent Definition", payload={"v": "short"}, source="prompt")

    updated = store.update(scope="s1", entry_id=entry.id, version=1, payload={"v": "x" * 400})

    assert updated.token_estimate > entry.token_estimate


def test_update_version_conflict_keeps_entry_unchanged(store):
    entry = store.write(scope="s1", category="Agent Definition", payload={"v": 1}, source="prompt")
    store.update(scope="s1", entry_id=entry.id, version=1, payload={"v": 2})

    with pytest.raises(VersionConflictError) as exc_info:
        store.update(scope="s1", entry_id=entry.id, version=1, payload={"v": 3})

    assert exc_info.value.current_version == 2
    loaded = store.load(scope="s1").entries[0]
    assert loaded.payload == {"v": 2}
    assert loaded.version == 2


def test_update_missing_entry_raises(store):
    with pytest.raises(EntryNotFoundError):
        store.update(scope="s1", entry_id="missing", version=1, payload={"v": 1})


def test_update_invalid_payload_does_not_bump_version(store):
    entry = store.write(scope="s1", category="Agent Definition", payload={"v": 1}, source="prompt")

    with pytest.raises(PayloadSerializationError):
        store.update(scope="s1", entry_id=entry.id, version=1, payload={"bad": UNSERIALIZABLE})

    assert store.load(scope="s1").entries[0].version == 1
