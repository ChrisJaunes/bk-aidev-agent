# -*- coding: utf-8 -*-
"""子资源加载器：每个来源一个模块，按 --resources 的顺序叠加（低 → 高优先级）。

==========  ===========================  ===========================================
名字        类                           来源
==========  ===========================  ===========================================
platform    PlatformConfigLoader         平台 agent 配置（底座，TTL 缓存）
injected    InjectedConfigLoader         注入的 agent_code / model（底座，无平台）
approval    ApprovalLoader               审批裁剪（approval_settings 置空）
local       LocalAgentConfigLoader       agents/<agent_code>.json（用户 + 项目两级）
mcp         McpLoader                    工作区 mcp.json 定义（ADD-only）
knowledge   KnowledgebaseLoader          per-agent 声明的知识库
workspace   WorkspaceFileLoader          工作区第一层的文件资源行
runtime     RuntimeInstanceLoader        进程级 current_model（/models 的选择）
==========  ===========================  ===========================================

名字与预设（纯标准库）在 aidev_agent_cli.resources.sources，入口解析参数时不必导入本包。

本包 ``__init__`` **不装载 platform**：``PlatformConfigLoader`` 顶部持有 SDK
（``AgentResourceManager``），而 ``loaders.base`` / 其余 loader 都是纯值或 stdlib ——
"import 一个纯值叶子却拉起 SDK" 是不该发生的传播。注册表因此按需构建：
:func:`registry` 在首次真要装配加载器链时把 platform 注册进来，:data:`PURE_LOADERS`
始终可直接使用。
"""

from __future__ import annotations

import importlib
from typing import Any, Iterable

from .approval import ApprovalLoader
from .base import ResourceLoader, ResourceOverrides
from .injected import InjectedConfigLoader
from .knowledge import KnowledgebaseLoader
from .local_agent import LocalAgentConfigLoader
from .mcp import McpLoader
from .runtime_state import RuntimeInstanceLoader
from .workspace import WorkspaceFileLoader

#: 纯 stdlib 的加载器（导入本包即已装载，可安全直接引用）。
PURE_LOADERS: dict[str, type[ResourceLoader]] = {
    cls.name: cls
    for cls in (
        InjectedConfigLoader,
        ApprovalLoader,
        LocalAgentConfigLoader,
        McpLoader,
        KnowledgebaseLoader,
        WorkspaceFileLoader,
        RuntimeInstanceLoader,
    )
}

#: 注册表缓存：``None`` 表示尚未构建（platform 是唯一需要按需装载的成员）。
_registry: dict[str, type[ResourceLoader]] | None = None


def registry() -> dict[str, type[ResourceLoader]]:
    """名字 → 加载器类的完整注册表（含 platform）。

    首次调用时装载 ``.platform`` —— 那是本包唯一持有 SDK 的加载器。纯值加载器不经过这条
    路径，因此 ``import aidev_agent_cli.resources.loaders.base`` 这类纯核引用不会拉起 SDK。
    """
    global _registry

    if _registry is None:
        # 显式动态装载：平台的加载器是本包唯一持有 SDK 的成员（``AgentResourceManager``），
        # 用 ``importlib`` 而不是函数内 ``from`` —— 后者会让 AST 审计无法区分「真按需」与
        # 「为绕环而延迟」。
        platform = importlib.import_module("aidev_agent_cli.resources.loaders.platform")
        loader = platform.PlatformConfigLoader
        _registry = {loader.name: loader, **PURE_LOADERS}

    return _registry


def get_loader_class(name: str) -> type[ResourceLoader]:
    """按名字取加载器类（含按需装载的 platform）。未知名字沿用注册表的 ``KeyError``。"""
    return registry()[name]


def build_loaders(rm: Any, names: Iterable[str]) -> list[ResourceLoader]:
    """按顺序为 rm 装配加载器链。names 须已经过 sources.parse_resources 校验。"""
    table = registry()

    return [table[name].create(rm) for name in names]


__all__ = [
    "PURE_LOADERS",
    "ApprovalLoader",
    "InjectedConfigLoader",
    "KnowledgebaseLoader",
    "LocalAgentConfigLoader",
    "McpLoader",
    "ResourceLoader",
    "ResourceOverrides",
    "RuntimeInstanceLoader",
    "WorkspaceFileLoader",
    "build_loaders",
    "get_loader_class",
    "registry",
]
