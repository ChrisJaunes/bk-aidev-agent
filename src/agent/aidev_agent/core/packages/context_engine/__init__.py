# -*- coding: utf-8 -*-
"""上下文分层存储（Offloading）包。

对外只暴露存储底座：数据模型、四组同步原语、注册式类别体系、冷热两层与统计。
本包不感知装配语义，不 import ``core.nodes`` / ``core.graphs``。
"""

from __future__ import annotations

from .cold import KIND_BACKUP, KIND_OFFLOADED, ColdBackend, InMemoryColdBackend
from .errors import (
    ContextStoreError,
    DuplicateCategoryError,
    EntryNotFoundError,
    EntryValidationError,
    PayloadSerializationError,
    UnknownCategoryError,
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
from .registry import CategoryRegistry, CategorySpec, build_builtin_registry
from .settings import StoreConfig
from .store import ContextStore

__all__ = [
    "KIND_BACKUP",
    "KIND_OFFLOADED",
    "CategoryRegistry",
    "CategorySpec",
    "ColdBackend",
    "ContextEntry",
    "ContextStore",
    "ContextStoreError",
    "DuplicateCategoryError",
    "EntryFilter",
    "EntryNotFoundError",
    "EntryStatus",
    "EntryValidationError",
    "InMemoryColdBackend",
    "Lifecycle",
    "LoadResult",
    "MaintenanceReport",
    "PayloadSerializationError",
    "StoreConfig",
    "StoreStats",
    "UnknownCategoryError",
    "VersionConflictError",
    "build_builtin_registry",
]
