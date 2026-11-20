# -*- coding: utf-8 -*-
"""注入来源（底座）：由调用方注入的 agent_code / model 合成 agent 配置，不触达平台。"""

from __future__ import annotations

from typing import Any

from .base import ResourceLoader


class InjectedConfigLoader(ResourceLoader):
    """无平台底座：产出与平台 retrieve_agent_config 同形状的 raw dict。

    同形状是关键——manager 的 get_agent_config 对所有底座只有一条映射路径
    （SDK 的 super().get_agent_config），叠加在上面的 local / mcp / knowledge / runtime
    来源因此对两种底座的语义完全一致。
    """

    name = "injected"
    is_base = True

    def __init__(self, rm: Any) -> None:
        self._rm = rm

    @classmethod
    def create(cls, rm: Any) -> "InjectedConfigLoader":
        return cls(rm)

    def apply(self, raw: dict[str, Any]) -> dict[str, Any]:
        request = getattr(self._rm, "_platform_request", None)
        agent_code = (request[0] if request else "") or self._rm.default_agent_code
        model = self._rm.default_model or self._rm.overrides.model or ""

        raw.setdefault("agent_code", agent_code)
        raw.setdefault("agent_name", agent_code)
        prompt = raw.setdefault("prompt_setting", {})
        if model:
            prompt.setdefault("llm_code", model)
            prompt.setdefault("non_thinking_llm", model)
        raw.setdefault("knowledgebase_settings", {"knowledgebases": []})
        raw.setdefault("mcp_server_config", {"mcpServers": {}})
        raw.setdefault("related_tools", [])
        raw.setdefault("related_skills", [])
        raw.setdefault("related_agents", [])

        return raw
