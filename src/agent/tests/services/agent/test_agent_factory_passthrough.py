# -*- coding: utf-8 -*-
"""注入点透传：``hook_handlers`` 与 ``interrupt_processor`` 经 ``agent_factory().create`` 落到实例。"""

from __future__ import annotations

import pytest
from aidev_agent.config import settings
from aidev_agent.core.hook import HookSet
from aidev_agent.enums import AgentBuildType
from aidev_agent.pydantic_models import AgentConfig
from aidev_agent.services.agent.factory import agent_factory
from langgraph.checkpoint.memory import MemorySaver


class _FakeResourceManager:
    """SDK 侧本地替身：满足 ``create`` 所需最小资源面，不依赖 CLI 包。

    ``ResourceManagerProtocol`` 明确支持鸭子类型实现，因此这里只提供构建期真正
    被读到的两个方法：``get_agent_config``（DIRECT 构建读配置）与
    ``get_chat_session_contents``（SESSION 构建读历史）。
    """

    def __init__(self, agent_code: str, chat_model: str) -> None:
        self.agent_code = agent_code
        self.chat_model = chat_model
        # ``build_executor_info`` 无条件读取这三个字段；替身没有平台凭证。
        self.app_code = ""
        self.app_secret = ""

    def resolve_access_token(self, username: str | None = None, **kwargs) -> str:
        return ""

    def get_agent_config(self, agent_code: str, version=None, **kwargs) -> AgentConfig:
        return AgentConfig.model_validate(
            {
                "agent_code": agent_code or self.agent_code,
                "agent_name": agent_code or self.agent_code,
                "chat_model": self.chat_model,
                "non_thinking_llm": self.chat_model,
                "tool_codes": [],
                "knowledgebase_ids": [],
                "knowledge_ids": [],
                "related_skills": [],
                "related_agents": [],
            }
        )

    def get_chat_session_contents(self, session_code: str, **kwargs) -> list[dict]:
        return []

    def construct_mcp(self, mcp_config: dict, username=None, **kwargs):
        """装配期无条件调用；无 MCP 配置就返回空结果，不触达平台。"""
        from aidev_agent.packages.langchain_core.tools.base import McpToolsResult

        assert not mcp_config, "本替身不覆盖 MCP 装配"
        return McpToolsResult(tools=[], fetch_failures=[])


@pytest.fixture(autouse=True)
def _endpoint():
    previous = settings.LLM_GW_ENDPOINT
    settings.set("LLM_GW_ENDPOINT", "http://127.0.0.1:9/v1")
    try:
        yield
    finally:
        settings.set("LLM_GW_ENDPOINT", previous)


def _create(**extra):
    return agent_factory().create(
        agent_code="agent-hook",
        build_type=AgentBuildType.DIRECT,
        session_context_data=[{"role": "user", "content": "hi"}],
        resource_manager=_FakeResourceManager(agent_code="agent-hook", chat_model="fake-model"),
        checkpointer=MemorySaver(),
        **extra,
    )


# ============================== 实例字段 ==============================


def test_create_lands_injected_fields_on_instance():
    handlers = HookSet({"tool_pre": [lambda ctx: None]})
    processor = object()

    agent = _create(hook_handlers=handlers, interrupt_processor=processor)

    assert agent.hook_handlers is handlers
    # build 不覆盖已注入的 interrupt_processor（execute 的惰性构造仅在 None 时生效）
    assert agent.interrupt_processor is processor


def test_create_without_injection_keeps_fields_none():
    agent = _create()

    assert agent.hook_handlers is None
    assert agent.interrupt_processor is None
