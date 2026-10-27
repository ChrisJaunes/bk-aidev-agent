# -*- coding: utf-8 -*-
"""上下文条目数据模型。

本模块只定义数据结构，不含任何存储行为与类别名；类别信息由注册表
（``registry.py``）声明，存储核心（``store.py``）按注册表驱动。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterator, Mapping, Sequence, Tuple


class Lifecycle(str, enum.Enum):
    """上下文条目的生命周期范围（有效跨度）。"""

    PLATFORM = "platform"
    AGENT = "agent"
    USER = "user"
    SESSION = "session"
    TURN = "turn"
    TASK = "task"
    REQUEST = "request"

    @classmethod
    def from_value(cls, value: "str | Lifecycle") -> "Lifecycle":
        """把字符串或枚举值规范化为 ``Lifecycle``；非法值抛 ``ValueError``。"""

        if isinstance(value, Lifecycle):
            return value
        if not isinstance(value, str):
            raise ValueError(f"非法生命周期: {value!r}")
        try:
            return cls(value.strip())
        except ValueError as exc:
            raise ValueError(f"非法生命周期: {value!r}") from exc


class EntryStatus(str, enum.Enum):
    """条目在存储中的状态。"""

    ACTIVE = "active"
    OFFLOADED = "offloaded"
    EVICTED = "evicted"


@dataclass
class ContextEntry:
    """承载一份原始上下文数据的最小存储单元。

    ``payload`` 在 ``offloaded`` / ``evicted`` 状态下为 ``None``，
    真实内容分别外置在冷层或已被物理删除。
    """

    id: str
    category: str
    source: str
    lifecycle: Lifecycle
    payload: Any = None
    status: EntryStatus = EntryStatus.ACTIVE
    version: int = 1
    sub_context: str = ""
    labels: Tuple[str, ...] = ()
    created_at: datetime | None = None
    updated_at: datetime | None = None
    last_accessed_at: datetime | None = None
    token_estimate: int = 0
    ref_count: int = 0

    def copy(self) -> "ContextEntry":
        """返回条目快照，避免调用方修改存储内部状态。"""

        return ContextEntry(
            id=self.id,
            category=self.category,
            source=self.source,
            lifecycle=self.lifecycle,
            payload=self.payload,
            status=self.status,
            version=self.version,
            sub_context=self.sub_context,
            labels=tuple(self.labels),
            created_at=self.created_at,
            updated_at=self.updated_at,
            last_accessed_at=self.last_accessed_at,
            token_estimate=self.token_estimate,
            ref_count=self.ref_count,
        )


@dataclass(frozen=True)
class EntryFilter:
    """加载/失效请求的过滤条件。

    维度内为「或」、维度间为「与」；``labels`` 采用交集非空判定；
    已 ``evicted`` 的条目永不返回。
    """

    categories: Sequence[str] = ()
    lifecycles: Sequence[Lifecycle] = ()
    labels: Sequence[str] = ()
    sources: Sequence[str] = ()
    include_offloaded: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", tuple(self.categories or ()))
        object.__setattr__(
            self,
            "lifecycles",
            tuple(Lifecycle.from_value(item) for item in (self.lifecycles or ())),
        )
        object.__setattr__(self, "labels", tuple(self.labels or ()))
        object.__setattr__(self, "sources", tuple(self.sources or ()))

    def matches(self, entry: ContextEntry) -> bool:
        """判断条目是否命中本过滤器（不做 ``evicted`` 判定，由调用方保证）。"""

        if self.categories and entry.category not in self.categories:
            return False
        if self.lifecycles and entry.lifecycle not in self.lifecycles:
            return False
        if self.sources and entry.source not in self.sources:
            return False
        if self.labels and not set(self.labels).intersection(entry.labels):
            return False
        return not (entry.status is EntryStatus.OFFLOADED and not self.include_offloaded)


@dataclass(frozen=True)
class LoadResult:
    """加载结果。

    ``degraded`` 表示冷层不可用或部分条目还原失败，返回数据可能不完整；
    ``skipped_ids`` 为还原失败被跳过的条目 id。
    """

    entries: Tuple[ContextEntry, ...] = ()
    degraded: bool = False
    skipped_ids: Tuple[str, ...] = ()

    def __iter__(self) -> Iterator[ContextEntry]:
        return iter(self.entries)

    def __len__(self) -> int:
        return len(self.entries)


@dataclass
class MaintenanceReport:
    """维护动作（sweep / end_*）的结果计数。"""

    offloaded: int = 0
    evicted: int = 0
    expired: int = 0
    deferred: int = 0
    failed: int = 0


@dataclass(frozen=True)
class StoreStats:
    """存储只读统计快照。

    取值本身不触发回捞、不修改 ``last_accessed_at``、不改动冷层记录内容。
    """

    hot_entries: int = 0
    offloaded_entries: int = 0
    backup_entries: int = 0
    entries_by_category: Mapping[str, int] = field(default_factory=dict)
    offload_count: int = 0
    rehydrate_count: int = 0
    eviction_count: int = 0
    total_token_estimate: int = 0
