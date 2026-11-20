# -*- coding: utf-8 -*-
"""Resource loaders merge raw SDK AgentConfig dictionaries in source order.

Platform/injected supplies the base; approval, per-agent declarations, MCP, knowledge,
workspace and explicit runtime overrides contribute independently. The SDK maps the
merged raw dictionary to its own AgentConfig; resource names never encode protocol choice.

本模块是 loaders 的**纯值叶子**：``ResourceOverrides`` 与 ``ResourceLoader`` 端口都不碰 SDK，
``resources.loaders.base`` 的 fresh import 因此零 SDK。需要 SDK 类型的 loader（platform /
manager）各自在顶部持有自己的 SDK 依赖——方向永远是 ``叶子 ← 实现``，不会反转。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass(frozen=True)
class ResourceOverrides:
    model: str = ""
    agent_code: str = ""


class ResourceLoader(abc.ABC):
    """子资源加载器的抽象基类。

    LocalResourceManager 按 --resources 给出的顺序（低 → 高优先级）叠加一组加载器；
    每个加载器只负责一个来源，契约：

    * name：--resources 里的名字（注册表键）；
    * is_base：是否为「底座」来源（platform / injected）——底座提供 agent 配置的基础
      dict，一条链里至多一个；
    * create(rm)：由 manager 装配实例（各加载器自己决定从 manager 取哪些协作者）；
    * apply(raw)：就地修改并返回同一个 raw dict，永不抛异常；日志只记键名/数量；
    * merge_to_raw(raw)：链遍历入口，默认委托给 apply。
    """

    name: ClassVar[str] = ""
    is_base: ClassVar[bool] = False

    @classmethod
    def create(cls, rm: Any) -> "ResourceLoader":
        return cls()

    @abc.abstractmethod
    def apply(self, raw: dict[str, Any]) -> dict[str, Any]: ...

    def merge_to_raw(self, raw: dict[str, Any]) -> dict[str, Any]:
        """链遍历入口：委托给 apply。"""
        return self.apply(raw)


__all__ = ["ResourceLoader", "ResourceOverrides"]

