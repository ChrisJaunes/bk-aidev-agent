# -*- coding: utf-8 -*-
"""上下文分层存储的异常层次。

全部异常继承 :class:`ContextStoreError`，调用方可按需捕获基类做整体降级，
也可按子类分别处理（例如版本冲突后重试）。
"""

from __future__ import annotations


class ContextStoreError(Exception):
    """上下文存储异常基类。"""


class UnknownCategoryError(ContextStoreError):
    """写入或过滤使用了未注册的上下文类别。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"未注册的上下文类别: {category!r}")


class DuplicateCategoryError(ContextStoreError):
    """注册重名类别。"""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(f"上下文类别已存在: {category!r}")


class EntryValidationError(ContextStoreError):
    """必填字段缺失、字段取值非法，或失效条件缺失。"""


class PayloadSerializationError(ContextStoreError):
    """负载无法 JSON 序列化。"""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"负载无法序列化: {detail}")


class VersionConflictError(ContextStoreError):
    """更新时版本不一致（并发冲突）。"""

    def __init__(self, entry_id: str, current_version: int) -> None:
        self.entry_id = entry_id
        self.current_version = current_version
        super().__init__(f"条目 {entry_id} 版本冲突，当前版本为 {current_version}")


class EntryNotFoundError(ContextStoreError):
    """条目不存在或已失效。"""

    def __init__(self, entry_id: str) -> None:
        self.entry_id = entry_id
        super().__init__(f"条目不存在或已失效: {entry_id}")
