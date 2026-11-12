# -*- coding: utf-8 -*-
"""hook 测试夹具：fake 模型、fake 工具与运行态绑定。"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig, RunnableLambda
from langchain_core.tools import tool

from aidev_agent.core.hook.registry import HookSet
from aidev_agent.core.hook.run_state import bind_run_state
from aidev_agent.core.hook.runner import HookRunner


class FakeLLM:
    """按顺序返回预置响应的 fake 模型，记录每次实际发送的消息。"""

    def __init__(self, responses: list[AIMessage]) -> None:
        self.responses = list(responses)
        self.calls: list[list] = []
        self.runnable = RunnableLambda(self._invoke, afunc=self._ainvoke)
        self.runnable.bind_tools = Mock(return_value=self.runnable)

    def _next(self, messages) -> AIMessage:
        self.calls.append(list(messages))
        return self.responses.pop(0) if self.responses else AIMessage(content="")

    def _invoke(self, messages, config=None, **kwargs) -> AIMessage:
        return self._next(messages)

    async def _ainvoke(self, messages, config=None, **kwargs) -> AIMessage:
        return self._next(messages)


@pytest.fixture
def fake_llm():
    """返回 FakeLLM 工厂。"""

    created: list[FakeLLM] = []

    def factory(responses: list[AIMessage]) -> FakeLLM:
        llm = FakeLLM(responses)
        created.append(llm)
        return llm

    return factory


@pytest.fixture
def search_tool():
    @tool
    def search(query: str) -> str:
        """Search for information"""
        return f"result for {query}"

    return search


@pytest.fixture
def build_model_node_with_hooks(monkeypatch, search_tool):
    """构造模型节点，并把提示词装配固定为单条 HumanMessage。"""
    from aidev_agent.core.nodes.model.node import build_model_node

    def factory(llm: FakeLLM, hooks=None, tools=None):
        prompt_template = Mock()
        prompt_value = Mock()
        prompt_value.to_messages.return_value = [HumanMessage(content="hello")]
        prompt_template.invoke = Mock(return_value=prompt_value)

        tool_list = list(tools if tools is not None else [search_tool])
        monkeypatch.setattr(
            "aidev_agent.core.nodes.model.node.ContextAssembly.get_chat_prompt_template",
            lambda self, ctx: prompt_template,
        )
        monkeypatch.setattr(
            "aidev_agent.core.nodes.model.node.ContextAssembly.get_chat_prompt_variables",
            lambda self, ctx: {"query": "hello", "chat_history": [], "agent_scratchpad": []},
        )
        monkeypatch.setattr(
            "aidev_agent.core.nodes.model.node.ContextAssembly.get_choice_tools",
            lambda self, ctx: tool_list,
        )
        return build_model_node(
            llm=llm.runnable,
            tools=tool_list,
            hooks=HookRunner(hooks) if hooks is not None else None,
        )

    return factory


def make_config(hooks: HookSet | None, *, run_id: str = "run-1") -> RunnableConfig:
    """构造带 hook 运行态的运行配置。"""
    config = RunnableConfig(configurable={})
    bind_run_state(config, hooks, agent_code="agent", session_code="session", run_id=run_id)
    return config


def run_node(node, config: RunnableConfig, messages=None):
    """以固定状态调用模型节点。"""
    state = {"messages": messages or [HumanMessage(content="hello")]}
    return node.invoke(state, config=config, store=Mock())
