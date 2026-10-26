# -*- coding: utf-8 -*-
"""T-16：构建期透传 hooks / hook_context / interrupt_processor（REQ-13）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from aidev_agent.config import settings
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.hooks import HookContextMiddleware
from aidev_agent.enums import AgentBuildType
from aidev_agent.headless.resource_manager import HeadlessResourceManager
from aidev_agent.pydantic_models import ExecuteKwargs, SecuritySettings
from aidev_agent.services.agent.factory import agent_factory
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver


class RecordingBus:
    def __init__(self):
        self.calls = []

    def enabled(self, point) -> bool:
        return True

    def points(self):
        return frozenset({"tool_pre", "tool_post", "session_start"})

    def emit(self, point, payload):
        self.calls.append((point, payload))
        return {}


class RecordingProcessor:
    """假的中断编排器（只需可辨识身份）。"""

    def __init__(self):
        self.dispatched = []


@tool
def _demo_tool(q: str) -> str:
    """demo."""
    return q


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
        resource_manager=HeadlessResourceManager(agent_code="agent-hook", chat_model="fake-model"),
        checkpointer=MemorySaver(),
        **extra,
    )


# ============================== 实例字段 ==============================


def test_create_lands_hook_fields_on_instance():
    bus = RecordingBus()
    processor = RecordingProcessor()

    agent = _create(hooks=bus, hook_context="注入上下文", interrupt_processor=processor)

    assert agent.hooks is bus
    assert agent.hook_context == "注入上下文"
    # build 不覆盖已注入的 interrupt_processor（execute 的惰性构造仅在 None 时生效）
    assert agent.interrupt_processor is processor


def test_create_without_hooks_keeps_fields_none():
    agent = _create()

    assert agent.hooks is None
    assert agent.hook_context is None
    assert agent.interrupt_processor is None


def test_get_agent_passes_hooks_and_context_to_executor():
    bus = RecordingBus()
    agent = _create(hooks=bus, hook_context="CTX")

    recorder = MagicMock(return_value=(MagicMock(), {}))
    agent.agent_cls = MagicMock()
    agent.agent_cls.get_agent_executor = recorder

    agent._get_agent([], execute_kwargs=ExecuteKwargs(stream=True))

    kwargs = recorder.call_args.kwargs
    assert kwargs["hooks"] is bus
    assert kwargs["hook_context"] == "CTX"


# ============================== builder 挂点 ==============================


def _builder(**kwargs) -> ReActAgentBuilder:
    builder = ReActAgentBuilder()
    builder._security_settings = SecuritySettings()
    for key, value in kwargs.items():
        setattr(builder, f"_{key}", value)
    return builder


def test_tool_node_receives_hooks():
    bus = RecordingBus()
    builder = _builder(hooks=bus, security_settings=SecuritySettings())

    with patch("aidev_agent.core.graphs.react.graph.build_tool_node") as build:
        builder._prepare_agent_tool_node([_demo_tool], langchain_middleware=())

    assert build.call_args.kwargs["hooks"] is bus


def test_tool_node_hooks_none_by_default():
    builder = _builder(security_settings=SecuritySettings())

    with patch("aidev_agent.core.graphs.react.graph.build_tool_node") as build:
        builder._prepare_agent_tool_node([_demo_tool], langchain_middleware=())

    assert build.call_args.kwargs["hooks"] is None


def test_model_node_appends_hook_context_middleware():
    builder = _builder(llm=MagicMock(), hook_context="CTX", security_settings=SecuritySettings())
    captured = {}

    def _capture(**kwargs):
        captured["options"] = kwargs["node_options"]
        return MagicMock()

    with patch("aidev_agent.core.graphs.react.graph.std_make_model_node", side_effect=_capture):
        builder._prepare_agent_model_node(
            llm=MagicMock(), non_thinking_llm=MagicMock(), judge_llm=None, tools=[]
        )

    middlewares = captured["options"].extra_template_middlewares
    assert any(isinstance(item, HookContextMiddleware) for item in middlewares)


def test_model_node_has_no_hook_middleware_without_context():
    builder = _builder(llm=MagicMock(), security_settings=SecuritySettings())
    captured = {}

    def _capture(**kwargs):
        captured["options"] = kwargs["node_options"]
        return MagicMock()

    with patch("aidev_agent.core.graphs.react.graph.std_make_model_node", side_effect=_capture):
        builder._prepare_agent_model_node(
            llm=MagicMock(), non_thinking_llm=MagicMock(), judge_llm=None, tools=[]
        )

    middlewares = captured["options"].extra_template_middlewares
    assert not any(isinstance(item, HookContextMiddleware) for item in middlewares)


def test_hook_context_middleware_appends_to_system_prompt():
    ctx = MagicMock()
    ctx.prompt_slots.system = "原始 prompt"
    called = []

    HookContextMiddleware(context="附加上下文")(ctx, lambda: called.append(1))

    assert ctx.prompt_slots.system == "原始 prompt\n\n附加上下文"
    assert called == [1]
