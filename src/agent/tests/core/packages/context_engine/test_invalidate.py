# -*- coding: utf-8 -*-
"""T-4 / REQ-4：失效（invalidate）的幂等与作用范围。"""

from __future__ import annotations

import pytest
from aidev_agent.core.packages.context_engine import EntryFilter, EntryValidationError, InMemoryColdBackend


def test_invalidate_by_id_returns_newly_invalidated_count(store):
    entry = store.write(scope="s1", category="Execution Trace", payload={"tool": "search"}, source="tool_call")

    assert store.invalidate(scope="s1", entry_ids=[entry.id]) == 1
    assert store.load(scope="s1").entries == ()


def test_invalidate_is_idempotent(store):
    entry = store.write(scope="s1", category="Execution Trace", payload={"tool": "search"}, source="tool_call")

    store.invalidate(scope="s1", entry_ids=[entry.id])

    assert store.invalidate(scope="s1", entry_ids=[entry.id]) == 0
    assert store.load(scope="s1").entries == ()


def test_invalidate_by_filter_only_touches_matches(store):
    trace = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    agent = store.write(scope="s1", category="Agent Definition", payload={"n": 2}, source="prompt")

    count = store.invalidate(scope="s1", entry_filter=EntryFilter(categories=("Execution Trace",)))

    assert count == 1
    remaining = store.load(scope="s1").entries
    assert [item.id for item in remaining] == [agent.id]
    assert trace.id != agent.id


def test_invalidate_removes_cold_record(make_store):
    cold = InMemoryColdBackend()
    store = make_store(cold=cold)
    entry = store.write(scope="s1", category="Execution Trace", payload={"tool": "search"}, source="tool_call")
    assert cold.count() == 1

    store.invalidate(scope="s1", entry_ids=[entry.id])

    assert cold.count() == 0


def test_invalidate_requires_condition(store):
    with pytest.raises(EntryValidationError):
        store.invalidate(scope="s1")


def test_invalidate_unknown_ids_are_noop(store):
    keeper = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")

    assert store.invalidate(scope="s1", entry_ids=["missing", keeper.id]) == 1
    assert store.load(scope="s1").entries == ()


def test_invalidate_does_not_cross_scope(store):
    entry = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    store.write(scope="s2", category="Execution Trace", payload={"n": 2}, source="tool_call")

    store.invalidate(scope="s1", entry_ids=[entry.id])

    assert len(store.load(scope="s2").entries) == 1
    assert store.load(scope="s1").entries == ()
