# -*- coding: utf-8 -*-
"""per-agent 本地声明来源：agents/<agent_code>.json。"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from .base import ResourceLoader
from ..local_config import LocalConfig, LocalDeclarations

logger = getLogger(__name__)


class LocalAgentConfigLoader(ResourceLoader):
    """per-agent 本地声明的加载器：``agents/${agent_code}.json`` 的全部字段 → 平台 raw dict。

    适配协作者：:class:`LocalDeclarations`——**复用**它读出的
    :class:`LocalConfig`，不重实现 per-agent 两级读取。

    命名（261014-urj D3）：原名 ``LocalConfigLoader``，改名 ``LocalAgentConfigLoader``
    以体现它读的是**单个 agent** 的配置（``agents/${code}.json``），与进程级的 TUI 配置
    （:class:`aidev_agent_cli.config.TuiConfig`）区分。``agent.json`` 这个固定文件名已退役。

    本加载器拥有 ``agents/${code}.json`` 贡献的一切，因为它就是那个文件的内容：

    * 顶层文本字段（``agent_id`` / ``tenant_id`` / ``agent_code`` /
      ``agent_name`` / ``space_id`` / ``agent_type`` / ``updated_by`` /
      ``service_catalogue``）按**平台词汇**写入 raw。规则是「**只增不删**」：
      文本字段一律**仅当本地值非空**时才写（不能把平台的 ``agent_name`` 清成
      ``""``）。⚠️ ``tenant_id`` / ``agent_type`` 的 dataclass 默认是
      ``"system"`` / ``"single"``，因此它们**总是**非空——写它们会盖掉平台值，这是
      **有意的**：用户声明了就是用户赢。
    * ``prompt_setting`` / ``conversation_settings``：**逐键并集**（本地键优先），
      仅当本地 section 非空时才写。注意 ``conversation_settings.commands`` 会被
      ``super()`` 读去构造 ``command_agent_mapping``——逐键并集对嵌套的 ``commands``
      列表是**整体替换**（本地那份覆盖平台那份），与旧行为一致。
    * **不碰** ``knowledgebase_settings``：``knowledgebases`` 的 ADD-only union 归
      :class:`KnowledgebaseLoader`（D2 的分工表），本加载器不处理该子键。
      **这是必须逐字保留的分工边界**：``LocalAgentConfigLoader`` 与
      :class:`KnowledgebaseLoader` 都读同一个 ``${code}.json``，若本加载器也处理
      ``knowledgebases`` 就会**双写**。
    * ``mcp_server_config``：**ENVELOPED 直通**（§6 顶部 E 节）。取
      :meth:`LocalDeclarations._mcp_servers` 得到 FLAT 映射，与
      ``raw["mcp_server_config"]["mcpServers"]`` 做 :meth:`LocalResourceManager.
      _merge_mcp_add_only`（ADD-only，用户裁决、mw9 定案，**不得回退为 REPLACE
      或普通 dict 覆盖**），包回 ``{"mcpServers": ...}`` 写回。

    读取（:meth:`read_config`，**注入缝**）
    --------------------------------------
    默认经 :meth:`LocalDeclarations.read` 按 per-agent 两级读取。这是 D1 的**正式注入缝**
    （代替已删除的构造参数）：``__init__`` 仍只收 ``rm``，测试可
    ``monkeypatch.setattr(type(loader), "read_config", lambda self, code: LocalConfig(...))``
    或对实例打补丁来注入声明，而不新增构造参数。

    子 agent 串味防护已由**路径隔离**取代（261014-urj 裁决 (b)）
    ---------------------------------------------------------
    ``_declaration_scope`` 门控与四段门控代码**已删除**。原因：``read_config`` 按
    ``agent_code`` 读**不同的文件**，父子 agent 物理上不可能互相串味——这是**结构性**
    保证（比运行时门控更强）。**空 ``agent_code``（``raw`` 无该键，少见）时读不到
    per-agent 文件，因此贡献空**——不再有"未限定作用域对任何拉取生效"的兜底（该兜底随
    门控一并删除）。
    """

    name = "local"

    @classmethod
    def create(cls, rm: Any) -> "LocalAgentConfigLoader":
        return cls(rm)

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def read_config(self, agent_code: str) -> LocalConfig:
        """读取该 ``agent_code`` 的 per-agent 本地声明（注入缝）。

        默认经 :meth:`LocalDeclarations.read` 的两级读取（用户空间 → 项目空间，项目覆盖
        用户）。``agent_code`` 为空或含路径穿越字符时返回空 :class:`LocalConfig`（由
        :meth:`LocalDeclarations.read` 处理）。
        """
        if not agent_code:
            return LocalConfig()

        return LocalDeclarations().read(code=agent_code)

    def declared_prompt_settings(self, local: LocalConfig) -> dict:
        """本地 ``prompt_setting`` section 的内容（空 section 时为 ``{}``）。

        收 ``local`` 参数（而非读实例状态），避免实例状态——这是本任务对原
        ``declared_prompt_settings()`` 的收参化。
        """
        section = local.prompt_setting

        return dict(section) if isinstance(section, dict) else {}

    def declared_servers(self, local: LocalConfig) -> dict[str, dict]:
        """``agents/${code}.json`` 的 ``mcp_server_config.mcpServers``（两级已合并）。"""
        return LocalDeclarations._mcp_servers(local)

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # per-agent 读取（裁决 (b) 后无门控）：raw 自带本次拉取的 agent_code，据此读
        # 对应的 agents/${code}.json。空 agent_code 读不到文件 → 贡献空。
        agent_code = raw.get("agent_code") or ""
        local = self.read_config(agent_code)

        if local.is_empty():
            return raw

        # 顶层文本字段：仅当本地值非空才写（不清空平台值）。``agent_id`` 只在 raw
        # 尚无该键时补（它是"新键"语义：平台通常不带 agent_id 时才由本地提供）。
        scalar_fields: tuple[tuple[str, Any], ...] = (
            ("tenant_id", local.tenant_id),
            ("agent_code", local.agent_code),
            ("agent_name", local.agent_name),
            ("space_id", local.space_id),
            ("agent_type", local.agent_type),
            ("updated_by", local.updated_by),
            ("service_catalogue", local.service_catalogue),
        )

        raw.update({key: value for key, value in scalar_fields if value})

        if local.agent_id and "agent_id" not in raw:
            raw["agent_id"] = local.agent_id

        # 嵌套 section：逐键并集（本地键优先），仅当本地 section 非空。
        for key, section in (
            ("prompt_setting", local.prompt_setting),
            ("conversation_settings", local.conversation_settings),
        ):
            if isinstance(section, dict) and section:
                raw[key] = {**(raw.get(key) or {}), **section}

        # MCP 声明：ENVELOPED 直通（§6 顶部 E 节）——与平台的 ``mcpServers`` 做
        # ADD-only 合并后包回信封。
        declared_servers = self.declared_servers(local)

        if declared_servers:
            mcp = raw.get("mcp_server_config")

            if not isinstance(mcp, dict):
                mcp = raw["mcp_server_config"] = {}

            servers = mcp.get("mcpServers")

            if not isinstance(servers, dict):
                servers = {}

            merged = self._rm._merge_mcp_add_only(dict(servers), declared_servers)
            added = sorted(name for name in merged if name not in servers)

            # 只有值真的变化才记；日志只记键名/数量，绝不记 server dict 内容。
            if added:
                logger.info(f"local declaration adds {len(added)} mcp server(s): {added}")

            mcp["mcpServers"] = merged

        # 主模型覆盖：记录与旧 ``apply(cfg)`` 逐字一致的语义（只覆盖主模型，
        # 仅当声明值非空、且与现值不同才记录——但实际生效由 ``super()`` 读
        # ``prompt_setting.llm_code`` 完成，这里只记日志）。
        declared_model = self.declared_prompt_settings(local).get("llm_code") or ""
        current_model = (raw.get("prompt_setting") or {}).get("llm_code") or ""

        if declared_model and declared_model != current_model:
            logger.info("overriding chat_model (local declaration)")

        return raw
