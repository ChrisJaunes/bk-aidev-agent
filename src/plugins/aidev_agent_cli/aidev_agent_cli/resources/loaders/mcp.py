# -*- coding: utf-8 -*-
"""MCP 定义来源：工作区 mcp.json（ADD-only）。"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from .base import ResourceLoader

logger = getLogger(__name__)


class McpLoader(ResourceLoader):
    """MCP 的加载器：**仅** ``mcp.json`` 的完整定义 → 平台 raw dict，ADD-only。

    适配协作者：:class:`McpResolver`（``mcp.json`` 的完整定义）。

    分工（用户裁决）：``agents/${code}.json`` 的 MCP **声明**由
    :class:`LocalAgentConfigLoader` 负责（那是本地配置文件的内容）；本加载器只处理
    ``mcp.json`` 的**定义**，不再重复处理声明。

    raw 层形状是 **ENVELOPED**
    -------------------------
    ``AgentConfig.mcp_server_config`` 是 **FLAT**（``名称 → 定义``：SDK ``chat.py`` 对
    每个具体 MCP 做 ``config.mcp_server_config.get(each)``），但**平台 raw dict** 里是
    **ENVELOPED**（``{"mcpServers": {...}}``）——``super().get_agent_config`` 正是在装配
    时做 ``.get("mcpServers", {})`` 拆封。因此本加载器在 raw 层写入 ``raw
    ["mcp_server_config"]["mcpServers"]``（ENVELOPED），**不**写 FLAT。在 raw 层写 FLAT
    会让 ``super()`` 拆出空 dict，具体 MCP 被静默过滤。§2 在读取 ``mcp.json`` 时完成
    拆封，本加载器拿到的是 FLAT 定义，包回信封后入库。

    合并规则（用户裁决，mw9 定案，**不得回退为 REPLACE**）：所有来源并集，同名条目
    先到先得、绝不互相替换；唯一例外是「定义可以补全占位」——先出现者只是 ``{}``
    占位、而后出现者带完整定义时，用后者补全。经
    :meth:`LocalResourceManager._merge_mcp_add_only`。

    平台绑定的取舍（``platform_mcp_disabled()`` 全有或全无）**不在此处**：那是
    平台配置域的事，由 :class:`PlatformConfigLoader` 在取数后应用。
    """

    name = "mcp"

    @classmethod
    def create(cls, rm: Any) -> "McpLoader":
        return cls(rm)

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def definitions(self) -> dict[str, dict]:
        """``mcp.json`` 的完整定义（经 :class:`McpResolver`，lazy + mtime 门控）。"""
        return self._rm.mcp.load().server_definitions()

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # 无门控（裁决 (b) 已删除）：``mcp.json`` 是**工作区**级全局来源（不是 per-agent
        # 配置），因此对它不做 per-agent 读取——它对该 workspace 的每次拉取都生效。
        definitions = self.definitions()

        if not definitions:
            return raw

        mcp = raw.get("mcp_server_config")

        if not isinstance(mcp, dict):
            mcp = raw["mcp_server_config"] = {}

        servers = mcp.get("mcpServers")

        if not isinstance(servers, dict):
            servers = {}

        before = set(servers)
        merged = self._rm._merge_mcp_add_only(dict(servers), definitions)
        added = [name for name in definitions if name not in before]

        if added:
            logger.info(f"mcp.json adds {len(added)} server(s): {sorted(added)}")

        mcp["mcpServers"] = merged

        return raw
