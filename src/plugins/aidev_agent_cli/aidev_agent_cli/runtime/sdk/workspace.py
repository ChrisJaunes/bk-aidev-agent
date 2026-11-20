# -*- coding: utf-8 -*-
"""本地文件 agent：``WorkspaceAgent`` 在 executor 构建期启用本地文件 runtime。

这是**运行时装配**，不是资源加载：它把进程里「用哪个通用 agent 类」换成本地文件版本，
并按 :func:`os.getcwd` 注册 ``local`` backend。资源解析（``--resources`` 的名字、加载器链、
管理器）与它无关，因此它住在 ``runtime`` 而不在 ``resources``。

**为何不能把本模块的名字提到 ``runtime/__init__`` 的导入面**：本模块顶层导入 SDK
（``CommonQAAgent`` / ``ReActAgentBuilder``），而包内模块在导入期拉入 SDK，因此 ``runtime``
包本身仍保持「导入不拉起 SDK」的约定。
"""

from __future__ import annotations

import os
from logging import getLogger
from typing import Any

from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.pydantic_models import AgentExecutorKwargs
from aidev_agent.services.common_agent import CommonQAAgent, common_agent_factory

logger = getLogger(__name__)

#: 本地 runtime 的路由名。工具的 ``target_runtime`` 参数按它解析 backend
#: （``RuntimeBackendResolver.resolve_backend``）。
LOCAL_RUNTIME_NAME = "local"


class WorkspaceAgent(CommonQAAgent):
    """本进程的通用 agent：在 executor 构建期启用本地文件 runtime。

    SDK 的官方扩展点（``common_agent/agent.py`` docstring）：继承
    :class:`CommonQAAgent` 覆写 :meth:`get_agent_executor`，由启动路径在
    ``bootstrap_environment`` 之后经 ``common_agent_factory.replace_defaults`` 注入。
    除本地 runtime 外，构建行为与父类逐字一致（同一 ``ReActAgentBuilder`` 链，含父类的
    ``enable_security_runtime(False)``）。

    根目录固定为 :func:`os.getcwd`（进程启动时的工作目录）。``options.runtime_backend_resolver``
    缺失时（DIRECT 构建路径）保持父类原样——那不是错误。

    ``virtual_mode=True`` 是硬编码的安全边界——默认 ``False`` 时绝对路径与 ``..`` 可逃逸
    ``root_dir``，agent 将获得全盘读写（SDK docstring 明确警告）。
    """

    @classmethod
    def get_agent_executor(cls, **kwargs: Any) -> tuple[Any, Any]:
        options = AgentExecutorKwargs.model_validate(kwargs)
        builder = ReActAgentBuilder().set_bkai_options(options).enable_security_runtime(False)

        project_root = os.getcwd()
        resolver = getattr(options, "runtime_backend_resolver", None)

        if resolver is not None:
            resolver.register_runtime(
                LOCAL_RUNTIME_NAME,
                FilesystemBackend(root_dir=project_root, virtual_mode=True),
            )
            builder.enable_runtime_local(True).set_enable_runtime_tool(True)
            logger.info(f"local file runtime enabled: runtime '{LOCAL_RUNTIME_NAME}' rooted at {project_root}")
        else:
            logger.info("local file runtime not enabled: resolver is None")

        agent, cfg = builder.build()

        return agent, cfg


def install_workspace_agent() -> None:
    """把 :class:`WorkspaceAgent` 注册为进程默认通用 agent（本地文件 runtime）。

    经 SDK 官方扩展点 ``common_agent_factory.replace_defaults``。进程级、只调一次：由入口在
    启动钩子之后无条件调用——与协议、``--resources`` 都无关。
    """
    common_agent_factory.replace_defaults(WorkspaceAgent())
    logger.info("default common agent replaced: WorkspaceAgent (local file runtime)")
