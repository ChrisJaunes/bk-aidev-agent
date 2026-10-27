# -*- coding: utf-8 -*-
"""T-2 / REQ-1、REQ-9：数据模型、类别注册表与「核心无类别名」约束。"""

from __future__ import annotations

from pathlib import Path

import pytest
from aidev_agent.core.packages.context_engine import (
    CategorySpec,
    DuplicateCategoryError,
    Lifecycle,
    UnknownCategoryError,
    build_builtin_registry,
)
from aidev_agent.core.packages.context_engine import store as store_module
from aidev_agent.core.packages.context_engine.registry import builtin_category_names

# (类别, 默认生命周期, 淘汰优先级, 是否永不淘汰) —— 与 plan.md 表格逐项一致
EXPECTED_BUILTINS = [
    ("Execution Trace", Lifecycle.TURN, 100, False),
    ("Current User Input", Lifecycle.TURN, 90, False),
    ("Environment & Governance", Lifecycle.REQUEST, 80, False),
    ("Working State", Lifecycle.TASK, 70, False),
    ("Conversation History", Lifecycle.SESSION, 60, False),
    ("Skill Context", Lifecycle.AGENT, 50, False),
    ("User Context", Lifecycle.USER, 40, False),
    ("Tool Definitions", Lifecycle.AGENT, 30, False),
    ("Agent Definition", Lifecycle.AGENT, 20, False),
    ("System Instructions", Lifecycle.PLATFORM, 0, True),
]


def test_builtin_registry_declares_ten_categories_in_order():
    registry = build_builtin_registry()

    assert len(registry) == 10
    assert registry.names() == tuple(name for name, *_ in EXPECTED_BUILTINS)
    assert builtin_category_names() == registry.names()


@pytest.mark.parametrize(("name", "lifecycle", "priority", "never_evict"), EXPECTED_BUILTINS)
def test_builtin_category_matches_plan_table(name, lifecycle, priority, never_evict):
    spec = build_builtin_registry().get(name)

    assert spec.default_lifecycle is lifecycle
    assert spec.eviction_priority == priority
    assert spec.never_evict is never_evict
    assert spec.sources, "典型来源应非空（说明性字段）"


def test_only_system_instructions_is_never_evict():
    registry = build_builtin_registry()

    never_evict_names = [spec.name for spec in registry.specs() if spec.never_evict]
    assert never_evict_names == ["System Instructions"]


def test_duplicate_registration_rejected_and_existing_untouched():
    registry = build_builtin_registry()
    original = registry.get("Working State")

    with pytest.raises(DuplicateCategoryError):
        registry.register(CategorySpec(name="Working State", default_lifecycle=Lifecycle.TURN, eviction_priority=99))

    assert registry.get("Working State") is original
    assert len(registry) == 10


def test_unknown_category_lookup_raises():
    with pytest.raises(UnknownCategoryError):
        build_builtin_registry().get("Not Registered")


def test_write_with_unknown_category_rejected_without_side_effect(store):
    before = store.stats()

    with pytest.raises(UnknownCategoryError):
        store.write(scope="s", category="Not Registered", payload={"a": 1}, source="unit-test")

    after = store.stats()
    assert (after.hot_entries, after.total_token_estimate) == (before.hot_entries, before.total_token_estimate)


def test_register_category_enables_write_and_load(store):
    spec = CategorySpec(
        name="Memory Context",
        default_lifecycle=Lifecycle.SESSION,
        sources=("memory",),
        eviction_priority=55,
    )
    store.register_category(spec)

    assert "Memory Context" in store.category_names()
    entry = store.write(scope="s", category="Memory Context", payload={"fact": "用户偏好中文"}, source="memory")
    loaded = store.load(scope="s").entries

    assert entry.category == "Memory Context"
    assert [item.payload for item in loaded] == [{"fact": "用户偏好中文"}]


def test_store_core_contains_no_category_name():
    """REQ-9：存储核心不得按类别名分支，十类名称只能出现在注册表。"""

    source = Path(store_module.__file__).read_text(encoding="utf-8")

    for name, *_ in EXPECTED_BUILTINS:
        assert name not in source, f"store.py 不应出现类别名 {name!r}"
