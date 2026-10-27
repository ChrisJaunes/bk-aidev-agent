# -*- coding: utf-8 -*-
"""上下文类别注册表。

十类内置类别以**数据表**形式声明在本模块，存储核心 ``store.py`` 不出现任何
类别名分支：默认生命周期、典型来源、淘汰优先级、是否可淘汰、token 预估器
全部由注册表提供，新增类别只需注册。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .errors import DuplicateCategoryError, UnknownCategoryError
from .models import Lifecycle


def default_token_estimator(blob: str) -> int:
    """缺省 token 预估：与 ``token_compression._estimate_tokens`` 的字符口径一致。"""

    return max(1, len(blob) // 4)


@dataclass(frozen=True)
class CategorySpec:
    """一个上下文类别的声明。

    :param name: 类别名，写入与过滤均以此为准
    :param default_lifecycle: 写入未显式指定 ``lifecycle`` 时采用的生命周期
    :param sources: 典型来源，仅作说明性信息，不参与校验
    :param eviction_priority: 淘汰优先级，数值越高越先被卸载/淘汰
    :param never_evict: 为真时既不参与卸载也不参与淘汰
    :param token_estimator: 可选 token 预估器，入参为条目 JSON 串
    """

    name: str
    default_lifecycle: Lifecycle
    sources: Tuple[str, ...] = ()
    eviction_priority: int = 0
    never_evict: bool = False
    token_estimator: Optional[Callable[[str], int]] = field(default=None, compare=False)

    def estimate_tokens(self, blob: str) -> int:
        if self.token_estimator is None:
            return default_token_estimator(blob)
        return max(1, int(self.token_estimator(blob)))


class CategoryRegistry:
    """线程安全的类别注册表。"""

    def __init__(self, specs: Optional[List[CategorySpec]] = None) -> None:
        self._lock = threading.Lock()
        self._specs: Dict[str, CategorySpec] = {}
        for spec in specs or ():
            self.register(spec)

    def register(self, spec: CategorySpec) -> None:
        """注册类别；重名抛 :class:`DuplicateCategoryError`，既有类别不受影响。"""

        if not isinstance(spec, CategorySpec):
            raise TypeError("spec 必须是 CategorySpec 实例")
        if not isinstance(spec.name, str) or not spec.name.strip():
            raise ValueError("类别名不能为空")
        with self._lock:
            if spec.name in self._specs:
                raise DuplicateCategoryError(spec.name)
            self._specs[spec.name] = spec

    def get(self, name: str) -> CategorySpec:
        """取类别声明；未注册抛 :class:`UnknownCategoryError`。"""

        with self._lock:
            spec = self._specs.get(name)
        if spec is None:
            raise UnknownCategoryError(name)
        return spec

    def __contains__(self, name: object) -> bool:
        with self._lock:
            return name in self._specs

    def names(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(self._specs.keys())

    def specs(self) -> Tuple[CategorySpec, ...]:
        with self._lock:
            return tuple(self._specs.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)


# 十类内置类别：名称、默认生命周期、典型来源、淘汰优先级与 plan.md 表格逐项一致。
_BUILTIN_SPECS: Tuple[CategorySpec, ...] = (
    CategorySpec(
        name="Execution Trace",
        default_lifecycle=Lifecycle.TURN,
        sources=("工具调用",),
        eviction_priority=100,
    ),
    CategorySpec(
        name="Current User Input",
        default_lifecycle=Lifecycle.TURN,
        sources=("用户请求",),
        eviction_priority=90,
    ),
    CategorySpec(
        name="Environment & Governance",
        default_lifecycle=Lifecycle.REQUEST,
        sources=("Policy Engine",),
        eviction_priority=80,
    ),
    CategorySpec(
        name="Working State",
        default_lifecycle=Lifecycle.TASK,
        sources=("State Store",),
        eviction_priority=70,
    ),
    CategorySpec(
        name="Conversation History",
        default_lifecycle=Lifecycle.SESSION,
        sources=("对话摘要、Handoff",),
        eviction_priority=60,
    ),
    CategorySpec(
        name="Skill Context",
        default_lifecycle=Lifecycle.AGENT,
        sources=("BKAI Agent Skill",),
        eviction_priority=50,
    ),
    CategorySpec(
        name="User Context",
        default_lifecycle=Lifecycle.USER,
        sources=("用户系统、会话",),
        eviction_priority=40,
    ),
    CategorySpec(
        name="Tool Definitions",
        default_lifecycle=Lifecycle.AGENT,
        sources=("BKAI Agent Tool/MCP",),
        eviction_priority=30,
    ),
    CategorySpec(
        name="Agent Definition",
        default_lifecycle=Lifecycle.AGENT,
        sources=("BKAI Agent Prompt/Role 配置",),
        eviction_priority=20,
    ),
    CategorySpec(
        name="System Instructions",
        default_lifecycle=Lifecycle.PLATFORM,
        sources=("SDK 直接注入的内容",),
        eviction_priority=0,
        never_evict=True,
    ),
)


def build_builtin_registry() -> CategoryRegistry:
    """返回装载十类内置类别的注册表。"""

    return CategoryRegistry(list(_BUILTIN_SPECS))


def builtin_category_names() -> Tuple[str, ...]:
    """十类内置类别名（顺序与 ``plan.md`` 表格一致）。"""

    return tuple(spec.name for spec in _BUILTIN_SPECS)
