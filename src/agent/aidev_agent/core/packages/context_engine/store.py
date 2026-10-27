# -*- coding: utf-8 -*-
"""上下文分层存储：写入 / 更新 / 失效 / 加载四组同步原语 + 分层维护。

设计要点（与 ``specs/context-offloading-storage/plan.md`` 对应）：

- **无类别分支**：类别属性一律经注册表查询，本模块不出现任何类别名。
- **全同步**：不建线程、不注册定时器；闲置卸载与容量治理由 ``sweep()`` 或写/更新/
  失效入口按 ``sweep_interval`` 节流驱动，到期清理由 ``end_*`` 显式触发。
- **冷层降级**：冷层异常一律捕获记 warning，不向上抛；读异常使 ``load`` 返回
  ``degraded=True``，写异常不影响写入路径。
- **无模块级容器**：条目只挂在实例属性上，实例可跨会话、跨轮次复用。
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import policies
from .cold import KIND_BACKUP, KIND_OFFLOADED, ColdBackend, InMemoryColdBackend
from .errors import (
    EntryNotFoundError,
    EntryValidationError,
    PayloadSerializationError,
    VersionConflictError,
)
from .models import (
    ContextEntry,
    EntryFilter,
    EntryStatus,
    Lifecycle,
    LoadResult,
    MaintenanceReport,
    StoreStats,
)
from .policies import ACTION_DEFER, ACTION_EVICT, ACTION_SKIP
from .registry import CategoryRegistry, CategorySpec, build_builtin_registry
from .settings import StoreConfig

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[ContextOffloading]"
# 连续失败达到该次数时升级为 error 级告警（REQ-10）
_CONSECUTIVE_FAILURE_LIMIT = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _serialize(payload: Any) -> str:
    """把负载序列化为冷层可存的 JSON 文本；失败抛 :class:`PayloadSerializationError`。"""

    try:
        return json.dumps(payload, ensure_ascii=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise PayloadSerializationError(str(exc)) from exc


def _require_non_empty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EntryValidationError(f"{field} 不能为空")
    return value


class ContextStore:
    """分层上下文存储。

    调用方构造并持有实例；实例内以 ``scope`` 做一级隔离，同一实例可承载多个
    会话/任务的条目。容量与阈值取 :class:`StoreConfig`，冷层默认进程内实现。
    """

    def __init__(
        self,
        *,
        config: Optional[StoreConfig] = None,
        registry: Optional[CategoryRegistry] = None,
        cold: Optional[ColdBackend] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._config = config if config is not None else StoreConfig.from_settings()
        self._registry = registry if registry is not None else build_builtin_registry()
        self._cold: ColdBackend = cold if cold is not None else InMemoryColdBackend()
        self._clock: Callable[[], datetime] = clock if clock is not None else _utcnow
        self._lock = threading.RLock()
        # scope -> entry_id -> ContextEntry
        self._entries: Dict[str, Dict[str, ContextEntry]] = {}
        self._last_sweep: Optional[datetime] = None
        self._offload_count = 0
        self._rehydrate_count = 0
        self._eviction_count = 0
        self._consecutive_failures = 0

    # ------------------------------------------------------------------ 注册

    def register_category(self, spec: CategorySpec) -> None:
        """注册新类别；注册成功即对写入与加载生效，核心代码无需修改。"""

        self._registry.register(spec)

    def category_names(self) -> Tuple[str, ...]:
        """已注册类别名（按注册顺序）。"""

        return self._registry.names()

    # ------------------------------------------------------------------ 写入

    def write(
        self,
        *,
        scope: str,
        category: str,
        payload: Any,
        source: str,
        lifecycle: Optional[Lifecycle] = None,
        sub_context: str = "",
        labels: Sequence[str] = (),
    ) -> ContextEntry:
        """写入一条上下文条目，返回条目快照。

        校验全部在落库前完成，任一失败都不触碰存储状态（无计数、无冷层写入）。
        """

        _require_non_empty_str(scope, "scope")
        _require_non_empty_str(category, "category")
        spec = self._registry.get(category)
        _require_non_empty_str(source, "source")
        if payload is None:
            raise EntryValidationError("payload 不能为空")
        if lifecycle is None:
            resolved_lifecycle = spec.default_lifecycle
        else:
            try:
                resolved_lifecycle = Lifecycle.from_value(lifecycle)
            except ValueError as exc:
                raise EntryValidationError(str(exc)) from exc
        if isinstance(labels, str) or not all(isinstance(label, str) for label in labels):
            raise EntryValidationError("labels 必须为字符串序列")
        if not isinstance(sub_context, str):
            raise EntryValidationError("sub_context 必须为字符串")

        blob = _serialize(payload)
        now = self._clock()
        entry = ContextEntry(
            id=uuid.uuid4().hex,
            category=category,
            source=source,
            lifecycle=resolved_lifecycle,
            payload=payload,
            status=EntryStatus.ACTIVE,
            version=1,
            sub_context=sub_context,
            labels=tuple(labels),
            created_at=now,
            updated_at=now,
            last_accessed_at=now,
            token_estimate=spec.estimate_tokens(blob),
        )

        with self._lock:
            self._entries.setdefault(scope, {})[entry.id] = entry
            self._write_backup(scope, entry, blob)

        self._maybe_maintain()
        return entry.copy()

    def update(self, *, scope: str, entry_id: str, version: int, payload: Any) -> ContextEntry:
        """按版本号更新条目 payload；版本不一致抛 :class:`VersionConflictError`。"""

        _require_non_empty_str(scope, "scope")
        _require_non_empty_str(entry_id, "entry_id")
        if not isinstance(version, int) or isinstance(version, bool):
            raise EntryValidationError("version 必须为整数")
        if payload is None:
            raise EntryValidationError("payload 不能为空")
        blob = _serialize(payload)

        with self._lock:
            entry = self._entries.get(scope, {}).get(entry_id)
            if entry is None or entry.status is EntryStatus.EVICTED:
                raise EntryNotFoundError(entry_id)
            if entry.version != version:
                raise VersionConflictError(entry_id, entry.version)

            if entry.status is EntryStatus.OFFLOADED:
                # 回捞失败不阻断更新：新 payload 由调用方提供，不依赖冷层内容
                self._rehydrate(scope, entry)

            spec = self._registry.get(entry.category)
            now = self._clock()
            entry.payload = payload
            entry.status = EntryStatus.ACTIVE
            entry.version += 1
            entry.updated_at = now
            entry.last_accessed_at = now
            entry.token_estimate = spec.estimate_tokens(blob)
            self._write_backup(scope, entry, blob)

        self._maybe_maintain()
        return entry.copy()

    def invalidate(
        self,
        *,
        scope: str,
        entry_ids: Optional[Sequence[str]] = None,
        entry_filter: Optional[EntryFilter] = None,
    ) -> int:
        """逻辑删除条目，返回**本次新失效**的条数（重复调用返回 0）。"""

        _require_non_empty_str(scope, "scope")
        if entry_ids is None and entry_filter is None:
            raise EntryValidationError("entry_ids 与 entry_filter 至少提供一项")
        if entry_ids is not None and isinstance(entry_ids, str):
            raise EntryValidationError("entry_ids 必须为字符串序列")

        with self._lock:
            bucket = self._entries.get(scope, {})
            if entry_ids is not None:
                targets = [
                    entry
                    for entry in (bucket.get(entry_id) for entry_id in dict.fromkeys(entry_ids))
                    if entry is not None and entry.status is not EntryStatus.EVICTED
                ]
            else:
                targets = [
                    entry
                    for entry in bucket.values()
                    if entry.status is not EntryStatus.EVICTED and entry_filter.matches(entry)
                ]

            for entry in targets:
                entry.status = EntryStatus.EVICTED
                entry.payload = None
                self._delete_cold(scope, entry.id)

        self._maybe_maintain()
        return len(targets)

    # ------------------------------------------------------------------ 加载

    def load(self, *, scope: str, entry_filter: Optional[EntryFilter] = None) -> LoadResult:
        """按过滤器加载条目；命中冷层条目时自动回捞，对调用方透明。"""

        _require_non_empty_str(scope, "scope")
        entry_filter = entry_filter if entry_filter is not None else EntryFilter()

        degraded = False
        skipped_ids: List[str] = []
        entries: List[ContextEntry] = []

        with self._lock:
            now = self._clock()
            for entry in list(self._entries.get(scope, {}).values()):
                if entry.status is EntryStatus.EVICTED:
                    continue
                if not entry_filter.matches(entry):
                    continue
                if entry.status is EntryStatus.OFFLOADED and not self._rehydrate(scope, entry):
                    degraded = True
                    skipped_ids.append(entry.id)
                    logger.warning(
                        "%s 降级 id=%s category=%s reason=cold_unavailable", _LOG_PREFIX, entry.id, entry.category
                    )
                    continue
                entry.last_accessed_at = now
                entries.append(entry.copy())

        return LoadResult(entries=tuple(entries), degraded=degraded, skipped_ids=tuple(skipped_ids))

    # ------------------------------------------------------------------ 引用

    def retain(self, *, scope: str, entry_ids: Sequence[str]) -> None:
        """增加条目引用计数（下限 0）；不影响其它字段与状态。"""

        self._adjust_ref_count(scope=scope, entry_ids=entry_ids, delta=1)

    def release(self, *, scope: str, entry_ids: Sequence[str]) -> None:
        """释放条目引用；``ref_count`` 为 0 后下次到期清理才生效。"""

        self._adjust_ref_count(scope=scope, entry_ids=entry_ids, delta=-1)

    # ------------------------------------------------------------------ 维护

    def end_turn(self, *, scope: str) -> MaintenanceReport:
        """轮次结束：清理当前轮（``turn``）条目。"""

        return self._expire(scope=scope, lifecycle=Lifecycle.TURN)

    def end_request(self, *, scope: str) -> MaintenanceReport:
        """请求结束：清理请求级（``request``）条目。"""

        return self._expire(scope=scope, lifecycle=Lifecycle.REQUEST)

    def end_session(self, *, scope: str) -> MaintenanceReport:
        """会话关闭：清理会话级（``session``）条目。"""

        return self._expire(scope=scope, lifecycle=Lifecycle.SESSION)

    def end_task(self, *, scope: str) -> MaintenanceReport:
        """任务终态：清理任务级（``task``）条目。"""

        return self._expire(scope=scope, lifecycle=Lifecycle.TASK)

    def sweep(self, *, scope: Optional[str] = None) -> MaintenanceReport:
        """执行一次维护：闲置卸载 → 热层容量治理 → 冷层容量淘汰。"""

        report = MaintenanceReport()
        with self._lock:
            now = self._clock()
            pairs = self._pairs(scope)

            # 1) 闲置卸载
            idle_candidates = policies.select_idle_offload_candidates(
                [entry for _, entry in pairs],
                now=now,
                idle_seconds=self._config.idle_seconds,
                resolve_priority=self._priority_of,
                is_never_evict=self._is_never_evict,
            )
            scope_of = self._scope_index(pairs)
            for entry in idle_candidates:
                if self._offload(scope_of[entry.id], entry):
                    report.offloaded += 1
                else:
                    report.failed += 1

            # 2) 热层容量治理（计数为实例级全局）
            all_pairs = self._pairs(None)
            all_scope_of = self._scope_index(all_pairs)
            hot_entries = [entry for _, entry in all_pairs if entry.status is EntryStatus.ACTIVE]
            overflow = len(hot_entries) - self._config.hot_capacity
            if overflow > 0:
                candidates = policies.select_capacity_candidates(
                    [entry for _, entry in pairs if entry.status is EntryStatus.ACTIVE],
                    overflow=overflow,
                    resolve_priority=self._priority_of,
                    is_never_evict=self._is_never_evict,
                )
                for entry in candidates:
                    if self._offload(all_scope_of[entry.id], entry):
                        report.offloaded += 1
                    else:
                        report.failed += 1
                if len(candidates) < overflow:
                    logger.warning(
                        "%s 热层超限 %s 条无法完全卸载：可卸载候选不足（不可卸载类别占满容量）",
                        _LOG_PREFIX,
                        overflow - len(candidates),
                    )

            # 3) 冷层容量淘汰
            cold_overflow = self._cold_offloaded_count() - self._config.cold_capacity
            if cold_overflow > 0:
                evict_candidates = policies.select_capacity_candidates(
                    [entry for _, entry in all_pairs if entry.status is EntryStatus.OFFLOADED],
                    overflow=cold_overflow,
                    resolve_priority=self._priority_of,
                    is_never_evict=self._is_never_evict,
                )
                for entry in evict_candidates:
                    try:
                        self._evict(all_scope_of[entry.id], entry)
                    except Exception as exc:  # 单条失败不影响其余条目
                        report.failed += 1
                        logger.warning("%s 淘汰失败 id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc)
                    else:
                        report.evicted += 1

            self._last_sweep = now
        return report

    def stats(self, *, scope: Optional[str] = None) -> StoreStats:
        """只读统计快照；不触发回捞、不修改任何条目状态。"""

        with self._lock:
            hot_entries = 0
            offloaded_entries = 0
            by_category: Dict[str, int] = {}
            total_tokens = 0
            for _, entry in self._pairs(scope):
                if entry.status is EntryStatus.EVICTED:
                    continue
                if entry.status is EntryStatus.ACTIVE:
                    hot_entries += 1
                else:
                    offloaded_entries += 1
                by_category[entry.category] = by_category.get(entry.category, 0) + 1
                total_tokens += entry.token_estimate

            return StoreStats(
                hot_entries=hot_entries,
                offloaded_entries=offloaded_entries,
                backup_entries=self._cold_backup_count(),
                entries_by_category=MappingProxyType(by_category),
                offload_count=self._offload_count,
                rehydrate_count=self._rehydrate_count,
                eviction_count=self._eviction_count,
                total_token_estimate=total_tokens,
            )

    # ------------------------------------------------------------ 内部：维护

    def _maybe_maintain(self) -> None:
        """按 ``sweep_interval`` 节流触发一次维护；时钟未推进时跳过。"""

        now = self._clock()
        if self._last_sweep is not None:
            elapsed = (now - self._last_sweep).total_seconds()
            if elapsed < self._config.sweep_interval:
                return
        self.sweep()

    def _expire(self, *, scope: str, lifecycle: Lifecycle) -> MaintenanceReport:
        """按生命周期清理 ``scope`` 内条目。"""

        _require_non_empty_str(scope, "scope")
        report = MaintenanceReport()
        with self._lock:
            targets = [entry for entry in list(self._entries.get(scope, {}).values()) if entry.lifecycle is lifecycle]
            for entry in targets:
                if entry.status is EntryStatus.EVICTED:
                    continue
                try:
                    spec = self._registry.get(entry.category)
                    action = policies.expiry_action(
                        eviction_priority=spec.eviction_priority,
                        never_evict=spec.never_evict,
                        ref_count=entry.ref_count,
                    )
                    if action == ACTION_SKIP:
                        continue
                    report.expired += 1
                    if action == ACTION_DEFER:
                        report.deferred += 1
                        logger.info(
                            "%s 到期延期 id=%s category=%s reason=referenced", _LOG_PREFIX, entry.id, entry.category
                        )
                        self._note_success()
                        continue
                    if action == ACTION_EVICT:
                        self._evict(scope, entry)
                        report.evicted += 1
                        logger.info(
                            "%s 到期淘汰 id=%s category=%s reason=expired", _LOG_PREFIX, entry.id, entry.category
                        )
                        self._note_success()
                        continue
                    if entry.status is EntryStatus.OFFLOADED:
                        report.offloaded += 1
                        self._note_success()
                        continue
                    if self._offload(scope, entry):
                        report.offloaded += 1
                        self._note_success()
                    else:
                        report.failed += 1
                        self._note_failure()
                except Exception as exc:  # 单条异常不影响其余条目
                    report.failed += 1
                    logger.warning("%s 到期清理失败 id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc)
                    self._note_failure()
        return report

    def _note_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= _CONSECUTIVE_FAILURE_LIMIT:
            logger.error("%s 到期清理连续失败 %s 次，请检查冷层可用性", _LOG_PREFIX, self._consecutive_failures)

    def _note_success(self) -> None:
        self._consecutive_failures = 0

    # ------------------------------------------------------------ 内部：分层

    def _offload(self, scope: str, entry: ContextEntry) -> bool:
        """把条目 payload 迁至冷层并清空热层内容；冷层不可用时保持原样并返回 False。"""

        try:
            record = self._cold.get(scope, entry.id)
            blob = record[1] if record is not None else _serialize(entry.payload)
        except Exception as exc:
            logger.warning(
                "%s 卸载失败（冷层不可用） id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc
            )
            return False

        try:
            self._cold.put(scope, entry.id, blob, KIND_OFFLOADED)
        except Exception as exc:
            logger.warning(
                "%s 卸载失败（冷层写入异常） id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc
            )
            return False

        entry.payload = None
        entry.status = EntryStatus.OFFLOADED
        self._offload_count += 1
        logger.info("%s 卸载 id=%s category=%s reason=idle_or_capacity", _LOG_PREFIX, entry.id, entry.category)
        return True

    def _rehydrate(self, scope: str, entry: ContextEntry) -> bool:
        """把冷层记录还原为热层 payload 并把记录翻回备份。"""

        try:
            record = self._cold.get(scope, entry.id)
        except Exception as exc:
            logger.warning(
                "%s 回捞失败（冷层不可用） id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc
            )
            return False
        if record is None:
            logger.warning("%s 回捞失败（冷层无记录） id=%s category=%s", _LOG_PREFIX, entry.id, entry.category)
            return False

        _, blob = record
        try:
            payload = json.loads(blob)
        except ValueError as exc:
            logger.warning("%s 回捞失败（记录损坏） id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc)
            return False

        entry.payload = payload
        entry.status = EntryStatus.ACTIVE
        entry.last_accessed_at = self._clock()
        self._rehydrate_count += 1
        try:
            self._cold.put(scope, entry.id, blob, KIND_BACKUP)
        except Exception as exc:  # 翻回备份失败不影响已还原的热层数据
            logger.warning("%s 备份翻转失败 id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc)
        logger.info("%s 回捞 id=%s category=%s", _LOG_PREFIX, entry.id, entry.category)
        return True

    def _evict(self, scope: str, entry: ContextEntry) -> None:
        """物理删除条目与冷层记录。"""

        self._delete_cold(scope, entry.id)
        self._entries.get(scope, {}).pop(entry.id, None)
        entry.status = EntryStatus.EVICTED
        entry.payload = None
        self._eviction_count += 1
        logger.info("%s 淘汰 id=%s category=%s reason=expired_or_capacity", _LOG_PREFIX, entry.id, entry.category)

    def _write_backup(self, scope: str, entry: ContextEntry, blob: str) -> None:
        try:
            self._cold.put(scope, entry.id, blob, KIND_BACKUP)
        except Exception as exc:  # 冷层写入失败不阻断写路径（REQ-7）
            logger.warning("%s 冷层备份失败 id=%s category=%s: %s", _LOG_PREFIX, entry.id, entry.category, exc)

    def _delete_cold(self, scope: str, entry_id: str) -> None:
        try:
            self._cold.delete(scope, entry_id)
        except Exception as exc:
            logger.warning("%s 冷层删除失败 id=%s: %s", _LOG_PREFIX, entry_id, exc)

    def _cold_offloaded_count(self) -> int:
        try:
            return self._cold.count(KIND_OFFLOADED)
        except Exception as exc:
            logger.warning("%s 冷层计数失败: %s", _LOG_PREFIX, exc)
            return sum(
                1
                for bucket in self._entries.values()
                for entry in bucket.values()
                if entry.status is EntryStatus.OFFLOADED
            )

    def _cold_backup_count(self) -> int:
        try:
            return self._cold.count(KIND_BACKUP)
        except Exception as exc:
            logger.warning("%s 冷层计数失败: %s", _LOG_PREFIX, exc)
            return 0

    # ------------------------------------------------------------ 内部：辅助

    def _adjust_ref_count(self, *, scope: str, entry_ids: Sequence[str], delta: int) -> None:
        _require_non_empty_str(scope, "scope")
        with self._lock:
            bucket = self._entries.get(scope, {})
            for entry_id in entry_ids:
                entry = bucket.get(entry_id)
                if entry is None or entry.status is EntryStatus.EVICTED:
                    continue
                entry.ref_count = max(0, entry.ref_count + delta)

    def _pairs(self, scope: Optional[str]) -> List[Tuple[str, ContextEntry]]:
        """展开 ``(scope, entry)`` 列表；``scope`` 为空时覆盖全部作用域。"""

        if scope is None:
            return [(scope_name, entry) for scope_name, bucket in self._entries.items() for entry in bucket.values()]
        return [(scope, entry) for entry in self._entries.get(scope, {}).values()]

    @staticmethod
    def _scope_index(pairs: Iterable[Tuple[str, ContextEntry]]) -> Dict[str, str]:
        return {entry.id: scope for scope, entry in pairs}

    def _priority_of(self, entry: ContextEntry) -> int:
        return self._registry.get(entry.category).eviction_priority

    def _is_never_evict(self, entry: ContextEntry) -> bool:
        return self._registry.get(entry.category).never_evict
