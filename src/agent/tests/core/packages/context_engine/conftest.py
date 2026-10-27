# -*- coding: utf-8 -*-
"""context_engine 测试公共夹具：可拨动时钟、store 工厂、十类样例数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest
from aidev_agent.core.packages.context_engine import ContextStore, InMemoryColdBackend
from aidev_agent.core.packages.context_engine.settings import StoreConfig

DEFAULT_CONFIG: Dict[str, Any] = {
    "hot_capacity": 100,
    "cold_capacity": 200,
    "idle_seconds": 900,
    "sweep_interval": 0,
}

_CONFIG_FIELDS = frozenset(DEFAULT_CONFIG)


class FakeClock:
    """可手动推进的 UTC 时钟，避免用例依赖 ``sleep``。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 1, 1, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


class ToggleColdBackend:
    """可开关故障的内存冷层：用于注入「冷层不可用」与「部分条目失败」场景。

    只存在于测试目录，不进入生产代码路径。
    """

    def __init__(self) -> None:
        self.inner = InMemoryColdBackend()
        self.fail_all = False
        self.fail_reads = False
        self.fail_writes = False
        self.fail_ids: set[str] = set()

    def _maybe_fail(self, entry_id: str) -> None:
        if entry_id in self.fail_ids:
            raise RuntimeError(f"cold backend rejected entry {entry_id}")

    def put(self, scope: str, entry_id: str, blob: str, kind: str) -> None:
        self._maybe_fail(entry_id)
        if self.fail_all or self.fail_writes:
            raise RuntimeError("cold backend unavailable (put)")
        self.inner.put(scope, entry_id, blob, kind)

    def get(self, scope: str, entry_id: str) -> tuple[str, str] | None:
        self._maybe_fail(entry_id)
        if self.fail_all or self.fail_reads:
            raise RuntimeError("cold backend unavailable (get)")
        return self.inner.get(scope, entry_id)

    def delete(self, scope: str, entry_id: str) -> None:
        self._maybe_fail(entry_id)
        if self.fail_all:
            raise RuntimeError("cold backend unavailable (delete)")
        self.inner.delete(scope, entry_id)

    def count(self, kind: str | None = None) -> int:
        if self.fail_all:
            raise RuntimeError("cold backend unavailable (count)")
        return self.inner.count(kind)


@pytest.fixture
def toggle_cold() -> ToggleColdBackend:
    return ToggleColdBackend()


@pytest.fixture
def make_store(clock: FakeClock):
    """构造 ``ContextStore``；容量/阈值等构造参数可直接覆盖默认值。"""

    def _factory(**overrides: Any) -> ContextStore:
        config_overrides = {key: overrides.pop(key) for key in list(overrides) if key in _CONFIG_FIELDS}
        overrides.setdefault("clock", clock)
        return ContextStore(config=StoreConfig(**{**DEFAULT_CONFIG, **config_overrides}), **overrides)

    return _factory


@pytest.fixture
def store(make_store) -> ContextStore:
    return make_store()


@pytest.fixture
def builtin_samples() -> List[Dict[str, Any]]:
    """十类样例数据的独立副本，避免用例之间互相污染。"""

    return [dict(sample) for sample in BUILTIN_SAMPLES]


# 十类内置类别的样例数据：类别、来源、负载、（可选）子上下文与标签。
# Working State 与 Environment & Governance 各覆盖 ≥2 个子上下文标签（AC-1）。
BUILTIN_SAMPLES: List[Dict[str, Any]] = [
    {
        "category": "System Instructions",
        "source": "sdk",
        "payload": {"instruction": "你是 IMate 助手，遵守安全策略。"},
    },
    {
        "category": "Agent Definition",
        "source": "bkai_agent_prompt",
        "payload": {"name": "研运 Agent", "role": "负责代码实现与验证"},
    },
    {
        "category": "Tool Definitions",
        "source": "bkai_agent_tool",
        "payload": {
            "name": "imate_issue_get",
            "parameters": {"type": "object", "properties": {"id": {"type": "string"}}},
        },
    },
    {
        "category": "Skill Context",
        "source": "bkai_agent_skill",
        "payload": {"skill": "imate-git", "usage": "按 clone 地址操作工蜂仓库"},
    },
    {
        "category": "User Context",
        "source": "user_system",
        "payload": {"user_id": "u-1", "tenant": "blueking", "role": "developer"},
    },
    {
        "category": "Conversation History",
        "source": "chat_summary",
        "payload": {
            "turns": [{"role": "user", "content": "实现分层存储"}, {"role": "assistant", "content": "已完成 T-1"}]
        },
    },
    {
        "category": "Current User Input",
        "source": "user_request",
        "payload": {"text": "请补一个按标签过滤的用例", "attachments": []},
    },
    {
        "category": "Execution Trace",
        "source": "tool_call",
        "payload": {"tool": "search", "observation": "命中 3 条结果"},
    },
    {
        "category": "Working State",
        "sub_context": "task_context",
        "labels": ("task_context",),
        "source": "state_store",
        "payload": {"goal": "完成代码实现", "deadline": "2026-10-30"},
    },
    {
        "category": "Working State",
        "sub_context": "planning_context",
        "labels": ("planning_context",),
        "source": "state_store",
        "payload": {"plan": ["T-1", "T-2"], "next": "T-3"},
    },
    {
        "category": "Environment & Governance",
        "sub_context": "human_feedback_context",
        "labels": ("human_feedback_context",),
        "source": "policy_engine",
        "payload": {"approval": "approved", "reviewer": "PM"},
    },
    {
        "category": "Environment & Governance",
        "sub_context": "environment_context",
        "labels": ("environment_context",),
        "source": "policy_engine",
        "payload": {"timezone": "Asia/Shanghai", "os": "linux"},
    },
]
