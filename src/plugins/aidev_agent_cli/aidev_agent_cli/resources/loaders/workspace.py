# -*- coding: utf-8 -*-
"""工作区文件来源：工作区第一层的文件资源行。"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from .base import ResourceLoader
from ..workspace import WorkspaceIndex

logger = getLogger(__name__)


class WorkspaceFileLoader(ResourceLoader):
    """工作空间文件的加载器：本地文件资源行。

    适配协作者：:class:`WorkspaceIndex`——**无状态适配**，包装由
    :class:`LocalResourceManager` 持有的那一个索引（``rm.files``）。
    在 rm 侧装配为 ``self.workspace_files``，因此全程**只有一个** ``WorkspaceIndex``
    实例（旧的「Session 与 rm 各持一个」双实例陷阱已消除）。

    它是**默认链的成员**（§3 装配），因此 ``retrieve_agent_config`` 自己就带着本地
    文件行，调用方不必再补。

    ``resources`` 为何可以进链
    --------------------------
    链的产物确实会流进 SDK 的 ``AgentConfig``（pydantic 模型），而 ``resources`` 不是它的
    字段——但这一点**不构成**排除的理由：``retrieve_agent_config`` 返回的是合并后的
    **raw dict**（`super().get_agent_config` 从中挑它认识的十余个键映射），多出的键只是
    不被映射，不会污染平台契约。真正会被"焊进契约"的是把 Node 侧载荷概念写进
    ``AgentConfig`` 的字段定义，而本加载器写的是 raw dict 上的额外键，两者不同。

    单次读取
    --------
    :meth:`project` 只 ``read()`` 一次，由同一次扫描派生全部文件行。
    """

    name = "workspace"

    @classmethod
    def create(cls, rm: Any) -> "WorkspaceFileLoader":
        # 复用 manager 持有的那一个（rm.workspace_files 与链成员必须是同一对象）。
        return rm.workspace_files

    def __init__(self, index: WorkspaceIndex) -> None:
        #: 被包装的工作区索引——rm 的 ``self.files``。
        self._index = index

    def project(self) -> list[dict[str, Any]]:
        """工作区根目录的文件资源行——**只读一次**工作区。

        待读取目录就是 :func:`os.getcwd`（由 :meth:`WorkspaceIndex.read` 决定），
        与会话 / 环境变量无关；``read`` / ``rows_for`` 的签名与对象同一性
        （``loader._index is rm.files``）保持不变。
        """
        return self._index.rows_for("")

    def file_resources(self) -> list[dict[str, Any]]:
        """工作区根目录的文件资源行——与 :meth:`project` 逐字等价。"""
        return self.project()

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        """把本地文件行并入 raw：**追加**到现有 ``resources`` 之后。

        追加而非覆盖：``resources`` 是平台自己的字段（工具行，如 ``weather_query``），
        本地工作区行排在它后面——与原先 ``describe_agent_config`` 的合并语义逐字一致。

        本地文件行**只**走 ``resources``（``type == "file"``）。此前同一份扫描还会额外写入
        一个 ``local_files`` 兄弟键，导致前端把同一个文件同时投影进「资源」与「文件」两个
        分组；该通道已删除。
        """
        raw["resources"] = [*self._existing_resources(raw), *self.project()]

        return raw

    @staticmethod
    def _existing_resources(raw: dict[str, Any]) -> list[dict[str, Any]]:
        """raw 里已有的 ``resources``（平台行）；缺失或非列表时为空。"""
        existing = raw.get("resources")

        return existing if isinstance(existing, list) else []
