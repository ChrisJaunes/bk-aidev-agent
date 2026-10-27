# -*- coding: utf-8 -*-
"""分层存储的纯策略函数。

本模块不持有状态、不触碰存储容器，只做「候选筛选 + 排序 + 动作判定」，
便于单测覆盖淘汰顺序与到期规则。调用方（``store.py``）以闭包形式传入
类别优先级解析器，因此这里不出现任何类别名。
"""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, List, Sequence, Tuple

from .models import ContextEntry, EntryStatus

# 到期后「直接淘汰」的优先级阈值：高于等于该值的类别到期即物理删除，其余卸载保留。
EXPIRY_EVICT_PRIORITY = 80

ACTION_OFFLOAD = "offload"
ACTION_EVICT = "evict"
ACTION_DEFER = "defer"
ACTION_SKIP = "skip"

PriorityResolver = Callable[[ContextEntry], int]
NeverEvictPredicate = Callable[[ContextEntry], bool]


def eviction_sort_key(
    *, eviction_priority: int, last_accessed_at: datetime | None, entry_id: str
) -> Tuple[int, float, str]:
    """候选排序键：优先级高者先处理 → 同级最久未用先处理 → 再按 id 保证稳定。"""

    timestamp = last_accessed_at.timestamp() if last_accessed_at is not None else 0.0
    return (-eviction_priority, timestamp, entry_id)


def idle_threshold_reached(*, now: datetime, last_accessed_at: datetime | None, idle_seconds: int) -> bool:
    """判断条目是否已超过闲置阈值。"""

    if last_accessed_at is None:
        return True
    return (now - last_accessed_at).total_seconds() >= idle_seconds


def sort_by_eviction_order(entries: Iterable[ContextEntry], resolve_priority: PriorityResolver) -> List[ContextEntry]:
    """按淘汰顺序排序（不筛选）。"""

    return sorted(
        entries,
        key=lambda entry: eviction_sort_key(
            eviction_priority=resolve_priority(entry),
            last_accessed_at=entry.last_accessed_at,
            entry_id=entry.id,
        ),
    )


def select_idle_offload_candidates(
    entries: Iterable[ContextEntry],
    *,
    now: datetime,
    idle_seconds: int,
    resolve_priority: PriorityResolver,
    is_never_evict: NeverEvictPredicate,
) -> List[ContextEntry]:
    """筛出「闲置且可卸载」的条目，并按淘汰顺序排序。

    仅 ``active`` 且仍持有 payload 的条目可被卸载；``never_evict`` 类别跳过。
    """

    candidates = [
        entry
        for entry in entries
        if entry.status is EntryStatus.ACTIVE
        and entry.payload is not None
        and not is_never_evict(entry)
        and idle_threshold_reached(now=now, last_accessed_at=entry.last_accessed_at, idle_seconds=idle_seconds)
    ]
    return sort_by_eviction_order(candidates, resolve_priority)


def select_capacity_candidates(
    entries: Sequence[ContextEntry],
    *,
    overflow: int,
    resolve_priority: PriorityResolver,
    is_never_evict: NeverEvictPredicate,
) -> List[ContextEntry]:
    """筛出容量治理要处理的条目：按淘汰顺序取前 ``overflow`` 个可处理条目。"""

    if overflow <= 0:
        return []
    candidates = [entry for entry in entries if not is_never_evict(entry)]
    return sort_by_eviction_order(candidates, resolve_priority)[:overflow]


def expiry_action(*, eviction_priority: int, never_evict: bool, ref_count: int) -> str:
    """到期条目的处置动作。

    - ``never_evict``：跳过，不卸载不淘汰；
    - 仍被引用（``ref_count > 0``）：延期到引用释放后的下一次清理；
    - 优先级不低于阈值：直接淘汰；
    - 其余：卸载至冷层保留。
    """

    if never_evict:
        return ACTION_SKIP
    if ref_count > 0:
        return ACTION_DEFER
    if eviction_priority >= EXPIRY_EVICT_PRIORITY:
        return ACTION_EVICT
    return ACTION_OFFLOAD
