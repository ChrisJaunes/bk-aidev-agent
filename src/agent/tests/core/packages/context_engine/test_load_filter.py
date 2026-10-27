# -*- coding: utf-8 -*-
"""T-5 / REQ-5、REQ-11：过滤加载语义与作用域隔离。"""

from __future__ import annotations

from aidev_agent.core.packages.context_engine import EntryFilter, Lifecycle


def _seed(store):
    """构造覆盖四个过滤维度的条目集合。"""

    entries = {
        "trace": store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call"),
        "input": store.write(
            scope="s1",
            category="Current User Input",
            payload={"n": 2},
            source="user_request",
            labels=("turn-input",),
        ),
        "history": store.write(scope="s1", category="Conversation History", payload={"n": 3}, source="chat_summary"),
        "tools": store.write(
            scope="s1",
            category="Tool Definitions",
            payload={"n": 4},
            source="bkai_agent_tool",
            labels=("lazy", "ptc"),
        ),
        "agent": store.write(scope="s1", category="Agent Definition", payload={"n": 5}, source="prompt"),
    }
    return entries


def test_filter_returns_exact_subset_for_turn_and_session(store):
    entries = _seed(store)

    result = store.load(scope="s1", entry_filter=EntryFilter(lifecycles=(Lifecycle.TURN, Lifecycle.SESSION)))

    assert {item.id for item in result.entries} == {entries["trace"].id, entries["input"].id, entries["history"].id}


def test_filter_returns_exact_subset_for_agent_level_tools(store):
    entries = _seed(store)

    result = store.load(
        scope="s1",
        entry_filter=EntryFilter(categories=("Tool Definitions",), lifecycles=(Lifecycle.AGENT,)),
    )

    assert [item.id for item in result.entries] == [entries["tools"].id]


def test_filter_dimensions_use_or_inside_and_across(store):
    entries = _seed(store)

    result = store.load(
        scope="s1",
        entry_filter=EntryFilter(
            categories=("Execution Trace", "Agent Definition"),
            lifecycles=(Lifecycle.TURN,),
        ),
    )

    # 维度内「或」+ 维度间「与」：Execution Trace（turn）命中，Agent Definition（agent）被生命周期排除
    assert [item.id for item in result.entries] == [entries["trace"].id]


def test_filter_sources_dimension(store):
    entries = _seed(store)

    result = store.load(scope="s1", entry_filter=EntryFilter(sources=("chat_summary", "prompt")))

    assert {item.id for item in result.entries} == {entries["history"].id, entries["agent"].id}


def test_filter_labels_use_intersection(store):
    entries = _seed(store)

    hit = store.load(scope="s1", entry_filter=EntryFilter(labels=("ptc", "unused")))
    miss = store.load(scope="s1", entry_filter=EntryFilter(labels=("unused",)))

    assert [item.id for item in hit.entries] == [entries["tools"].id]
    assert miss.entries == ()


def test_empty_match_returns_empty_result_not_error(store):
    _seed(store)

    result = store.load(scope="s1", entry_filter=EntryFilter(categories=("Skill Context",)))

    assert result.entries == ()
    assert result.degraded is False
    assert result.skipped_ids == ()


def test_evicted_entries_never_returned(store):
    entries = _seed(store)
    store.invalidate(scope="s1", entry_ids=[entries["trace"].id])

    result = store.load(scope="s1")

    assert entries["trace"].id not in {item.id for item in result.entries}
    assert len(result.entries) == len(entries) - 1


def test_scopes_are_isolated(store):
    s1_entry = store.write(scope="s1", category="Execution Trace", payload={"scope": 1}, source="tool_call")
    s2_entry = store.write(scope="s2", category="Execution Trace", payload={"scope": 2}, source="tool_call")

    s1_entries = store.load(scope="s1").entries
    s2_entries = store.load(scope="s2").entries

    assert [item.id for item in s1_entries] == [s1_entry.id]
    assert [item.id for item in s2_entries] == [s2_entry.id]
    assert store.load(scope="unknown").entries == ()


def test_load_refreshes_last_accessed_at(store, clock):
    entry = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    clock.advance(120)

    loaded = store.load(scope="s1").entries[0]

    assert loaded.id == entry.id
    assert loaded.last_accessed_at == clock.now


def test_filter_normalizes_sequence_inputs():
    entry_filter = EntryFilter(categories=["Agent Definition"], lifecycles=["agent"], labels=("a",), sources=["prompt"])

    assert entry_filter.categories == ("Agent Definition",)
    assert entry_filter.lifecycles == (Lifecycle.AGENT,)


def test_load_does_not_return_offloaded_when_excluded(store, clock):
    entry = store.write(scope="s1", category="Execution Trace", payload={"n": 1}, source="tool_call")
    clock.advance(1000)
    store.sweep(scope="s1")

    result = store.load(scope="s1", entry_filter=EntryFilter(include_offloaded=False))

    assert result.entries == ()
    assert entry.id not in {item.id for item in result.entries}
