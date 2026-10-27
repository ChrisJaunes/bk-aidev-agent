# -*- coding: utf-8 -*-
"""冷层协议与默认内存实现。

冷层只承载序列化后的 payload 文本，不理解负载语义。记录分两类 ``kind``：

- ``backup``：写入/更新成功后保留的同步副本，供热层条目意外丢失时兜底；
- ``offloaded``：热层条目被卸载后外置的内容，加载时回捞。

冷层容量只统计 ``offloaded``，备份条目数由热层容量间接约束。
"""

from __future__ import annotations

import threading
from typing import Dict, Protocol, Tuple, runtime_checkable

KIND_BACKUP = "backup"
KIND_OFFLOADED = "offloaded"


@runtime_checkable
class ColdBackend(Protocol):
    """冷层存储契约；替换为 Redis / 外置 KV 时只需实现这四个方法。"""

    def put(self, scope: str, entry_id: str, blob: str, kind: str) -> None: ...

    def get(self, scope: str, entry_id: str) -> Tuple[str, str] | None: ...

    def delete(self, scope: str, entry_id: str) -> None: ...

    def count(self, kind: str | None = None) -> int: ...


class InMemoryColdBackend:
    """进程内冷层实现。

    记录结构：``_records[(scope, entry_id)] = (kind, blob)``，
    同一 ``(scope, entry_id)`` 再次 ``put`` 覆盖旧记录。
    不落本地文件、不跨实例共享，重启即丢失——这是本期已确认的取舍。
    """

    def __init__(self) -> None:
        self._records: Dict[Tuple[str, str], Tuple[str, str]] = {}
        self._lock = threading.Lock()

    def put(self, scope: str, entry_id: str, blob: str, kind: str) -> None:
        with self._lock:
            self._records[(scope, entry_id)] = (kind, blob)

    def get(self, scope: str, entry_id: str) -> Tuple[str, str] | None:
        with self._lock:
            return self._records.get((scope, entry_id))

    def delete(self, scope: str, entry_id: str) -> None:
        with self._lock:
            self._records.pop((scope, entry_id), None)

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                return len(self._records)
            return sum(1 for record_kind, _ in self._records.values() if record_kind == kind)
