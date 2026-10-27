# -*- coding: utf-8 -*-
"""T-1：``AgentFactory`` 协议、默认实现与进程级注册中心。

覆盖 REQ-1 / REQ-2 / REQ-3 的协议面：
- 未替换时 ``agent_factory()`` 返回 ``DefaultAgentFactory``；
- ``replace_defaults(impl)`` 返回旧值并让注册中心改指新实现（与仓内 ``SingletonFactory`` 约定一致）；
- ``build_agent`` 与 ``create`` 对同一入参产出的 ``AgentBuildContext`` 字段全等（ACC-1 的协议侧）；
- ``_FACTORY_TOKEN`` 闸口保留：直接 ``AgentInstanceFactory(...)`` 仍抛原 ``RuntimeError``。
"""

from dataclasses import fields

import pytest
from aidev_agent.enums import AgentBuildType, AgentType
from aidev_agent.pydantic_models import AgentConfig
from aidev_agent.services.agent.factory import (
    AgentInstanceFactory,
    DefaultAgentFactory,
    agent_factory,
)
from aidev_agent.services.agent.registry import AgentBuildContext


class _StubResourceManager:
    """最小 RM：只提供 CHAT 装配需要的 ``get_agent_config``。"""

    username = "tester"

    def get_agent_config(self, agent_code: str, version=None, **kwargs) -> AgentConfig:
        return AgentConfig(
            agent_code=agent_code,
            agent_name=agent_code,
            chat_model="fake-model",
            non_thinking_llm="fake-model",
        )


class _CapturingAgent:
    """记录 build 期 ctx 的假 Agent 实现。"""

    def __init__(self, sink: "_FakeAgentRegistry"):
        self._sink = sink

    def build(self, ctx: AgentBuildContext):
        self._sink.contexts.append(ctx)
        return self


class _FakeAgentRegistry:
    """替换模块级 ``agent_registry``，避免真实注册表参与。"""

    def __init__(self):
        self.contexts: list[AgentBuildContext] = []

    def __contains__(self, agent_type):
        return True

    def must_get(self, agent_type):
        return lambda: _CapturingAgent(self)


_BUILD_PARAMS = dict(
    agent_code="agent-hook",
    agent_type=AgentType.CHAT,
    build_type=AgentBuildType.DIRECT,
    session_context_data=[{"role": "user", "content": "hi"}],
    temperature=0.1,
    max_tokens=256,
    resource_manager=_StubResourceManager(),
    username="tester",
    channel_type="chat",
)


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeAgentRegistry()
    monkeypatch.setattr("aidev_agent.services.agent.factory.agent_registry", registry)
    return registry


def test_default_factory_is_the_registry_default():
    assert isinstance(agent_factory(), DefaultAgentFactory)


def test_replace_defaults_returns_legacy_and_switches_impl():
    class _Custom(DefaultAgentFactory):
        pass

    legacy = agent_factory.replace_defaults(_Custom())
    try:
        assert isinstance(agent_factory(), _Custom)
        assert isinstance(legacy, DefaultAgentFactory)
    finally:
        agent_factory.replace_defaults(legacy)
    assert isinstance(agent_factory(), DefaultAgentFactory)


def test_factory_gate_still_rejects_direct_construction():
    with pytest.raises(RuntimeError, match="不可直接实例化"):
        AgentInstanceFactory(agent_code="agent-hook")


def test_build_agent_and_create_produce_equal_build_context(fake_registry):
    AgentInstanceFactory.build_agent(**_BUILD_PARAMS)
    DefaultAgentFactory().create(**_BUILD_PARAMS)

    via_build_agent, via_create = fake_registry.contexts
    for item in fields(AgentBuildContext):
        assert getattr(via_build_agent, item.name) == getattr(via_create, item.name), item.name
    assert via_create.extra == {"channel_type": "chat"}
    assert via_create.session_code is None
