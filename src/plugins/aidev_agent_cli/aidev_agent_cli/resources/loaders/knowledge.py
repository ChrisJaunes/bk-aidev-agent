# -*- coding: utf-8 -*-
"""知识库来源：per-agent 声明的 knowledgebases 增量追加。"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from .base import ResourceLoader
from ..local_config import LocalConfig, BkAiAgentInfo

logger = getLogger(__name__)


class KnowledgebaseLoader(ResourceLoader):
    """知识库的加载器：per-agent 声明增量追加到
    ``raw["knowledgebase_settings"]["knowledgebases"]``。

    适配协作者：:class:`BkAiAgentInfo`（**per-agent**，``agents/${code}.json``）。
    只追加平台尚无的名称、保持平台顺序在前——这是「本地资源叠加在 agent 配置之上」的
    读法，也是重构前的逐字语义。

    本地声明来源（261014-urj）：与 :class:`LocalAgentConfigLoader` 同法读
    ``read_config(agent_code)``（**同一个** ``${code}.json``）。**分工边界必须保留**：
    :class:`LocalAgentConfigLoader` **不碰** ``knowledgebase_settings`` 的
    ``knowledgebases`` 子键，由本加载器负责——否则会**双写**。

    写入位置：raw 层的 ``raw["knowledgebase_settings"]["knowledgebases"]``。语义逐字
    保留：平台在前、只追加本地新增、去重。``super()`` 随后用
    ``res["knowledgebase_settings"]["knowledgebases"]`` 装配 ``knowledgebase_ids``，
    因此断言的端到端结果不变。

    无子 agent 门控（裁决 (b) 已删除）：读取按 ``agent_code`` 逐文件，路径隔离即防护。
    """

    name = "knowledge"

    @classmethod
    def create(cls, rm: Any) -> "KnowledgebaseLoader":
        return cls(rm)

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    def read_config(self, agent_code: str) -> LocalConfig:
        """读取该 ``agent_code`` 的 per-agent 本地声明（与 :class:`LocalAgentConfigLoader`
        同法的注入缝）。"""
        if not agent_code:
            return LocalConfig()

        return BkAiAgentInfo().read(code=agent_code)

    def declared_knowledgebases(self, agent_code: str) -> list[str]:
        """per-agent ``knowledgebase_settings.knowledgebases`` 名称列表。"""
        section = self.read_config(agent_code).knowledgebase_settings

        if not isinstance(section, dict):
            return []

        names = section.get("knowledgebases")

        return [each for each in names if isinstance(each, str) and each] if isinstance(names, list) else []

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        # per-agent 读取（裁决 (b) 后无门控）：按本次拉取的 agent_code 读对应文件。
        agent_code = raw.get("agent_code") or ""
        kbs = self.declared_knowledgebases(agent_code)

        if not kbs:
            return raw

        kb = raw.get("knowledgebase_settings")

        if not isinstance(kb, dict):
            kb = raw["knowledgebase_settings"] = {}

        existing = kb.get("knowledgebases")

        if not isinstance(existing, list):
            existing = []

        added = [name for name in kbs if name not in existing]

        if added:
            logger.info(f"local config adds {len(added)} knowledgebase(s): {added}")

        kb["knowledgebases"] = list(existing) + added

        return raw
