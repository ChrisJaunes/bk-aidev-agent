# -*- coding: utf-8 -*-
"""T-3：``resume`` 协议与负载显式失败（REQ-1 / REQ-5 / ACC-4 的协议侧）。

- 空负载 → ``AgentFactoryError(code="resume_payload_missing")``；
- 非中断形状负载 → ``AgentFactoryError(code="resume_payload_unsupported")``，
  消息只带字段名与类型、不回显取值；
- 校验通过 → 走与 ``create`` 共用的装配链路，返回满足 ``AgentProtocol`` 的实例；
- 替换实现抛出的异常原样外抛，不回落默认实现。
"""

from dataclasses import fields

import pytest
from aidev_agent.enums import AgentBuildType, AgentType
from aidev_agent.exceptions import AgentFactoryError
from aidev_agent.pydantic_models import AgentConfig
from aidev_agent.services.agent.factory import DefaultAgentFactory, agent_factory
from aidev_agent.services.agent.registry import AgentBuildContext


class _StubResourceManager:
    username = "tester"

    def get_agent_config(self, agent_code: str, version=None, **kwargs) -> AgentConfig:
        return AgentConfig(
            agent_code=agent_code,
            agent_name=agent_code,
            chat_model="fake-model",
            non_thinking_llm="fake-model",
        )


class _FakeAgentRegistry:
    def __init__(self):
        self.contexts: list[AgentBuildContext] = []

    def __contains__(self, agent_type):
        return True

    def must_get(self, agent_type):
        registry = self

        class _Agent:
            def build(self, ctx):
                registry.contexts.append(ctx)
                return self

            def execute(self, execute_kwargs=None):  # AgentProtocol 运行时方法
                return ""

            def stop(self):
                return None

        return _Agent


@pytest.fixture
def fake_registry(monkeypatch):
    registry = _FakeAgentRegistry()
    monkeypatch.setattr("aidev_agent.services.agent.factory.agent_registry", registry)
    return registry


BUILD_PARAMS = dict(
    agent_code="agent-hook",
    agent_type=AgentType.CHAT,
    build_type=AgentBuildType.DIRECT,
    session_context_data=[{"role": "user", "content": "hi"}],
    resource_manager=_StubResourceManager(),
    username="tester",
)


@pytest.mark.parametrize("payload", [None, [], ()])
def test_empty_resume_payload_raises_missing(payload):
    with pytest.raises(AgentFactoryError) as exc_info:
        DefaultAgentFactory().resume(session_code="sess-1", resume=payload, **BUILD_PARAMS)
    assert exc_info.value.code == "resume_payload_missing"
    assert exc_info.value.reason


@pytest.mark.parametrize(
    "payload",
    [
        "resolved",
        [{"status": "resolved", "payload": {"answers": ["secret-answer"]}}],
        [{"interruptId": "i-1"}, "unexpected"],
        {"status": "resolved"},
    ],
)
def test_non_interrupt_shape_raises_unsupported_without_values(payload):
    with pytest.raises(AgentFactoryError) as exc_info:
        DefaultAgentFactory().resume(session_code="sess-1", resume=payload, **BUILD_PARAMS)
    assert exc_info.value.code == "resume_payload_unsupported"
    # 只带字段名与类型，绝不回显负载取值
    assert "secret-answer" not in exc_info.value.reason
    assert "interruptId" in exc_info.value.reason or "list" in exc_info.value.reason or "dict" in exc_info.value.reason


def test_valid_interrupt_payload_builds_instance(fake_registry):
    agent = DefaultAgentFactory().resume(
        session_code="sess-resume",
        resume={"interruptId": "i-1", "status": "resolved", "payload": {"answers": [{"q": 1}]}},
        **BUILD_PARAMS,
    )
    assert hasattr(agent, "execute") and hasattr(agent, "stop")
    (ctx,) = fake_registry.contexts
    assert ctx.session_code == "sess-resume"
    assert ctx.session_context_data == [{"role": "user", "content": "hi"}]
    # 装配上下文与 create 同源：字段集合一致
    assert {item.name for item in fields(AgentBuildContext)} == set(vars(ctx))


def test_replaced_factory_exception_propagates_without_fallback():
    class _Broken(DefaultAgentFactory):
        def resume(self, *, session_code, resume, **build_params):
            raise AgentFactoryError(code="agent.unsupported", reason="注入实现不支持该恢复类型")

        def create(self, **build_params):  # 若发生回落，本方法会被调用
            raise AssertionError("不得回落到默认实现")

    legacy = agent_factory.replace_defaults(_Broken())
    try:
        with pytest.raises(AgentFactoryError) as exc_info:
            agent_factory().resume(session_code="sess-1", resume={"interruptId": "i-1"})
        assert exc_info.value.code == "agent.unsupported"
    finally:
        agent_factory.replace_defaults(legacy)
