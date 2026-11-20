# -*- coding: utf-8 -*-
"""T-2：创建入口委派与调用方零改动校验（ACC-2 的协议侧）。

仓内 5 处创建调用（``services/agent_bkplugin.py`` ×2、``services/agent_builder.py``、
``views/chat.py``、``aidev_wxbot/wxaibot/strategies.py``）都只经过
``AgentInstanceFactory.build_agent`` 的关键字入参。这里用记录型 ``AgentFactory``
替换默认实现，断言这些入参形状全部命中 ``create``（而不是 ``resume``），
并复核非法 ``build_type`` / ``agent_type`` 的异常语义与基线一致。
"""

import pytest
from aidev_agent.enums import AgentBuildType, AgentType
from aidev_agent.exceptions import AgentFactoryError
from aidev_agent.services.agent.factory import AgentInstanceFactory, DefaultAgentFactory, agent_factory


# 与 agent_bkplugin:112 / agent_builder:209 同形的 CHAT 构建调用（DIRECT/SESSION 只差 build_type）
CHAT_CALL = dict(
    build_type=AgentBuildType.DIRECT,
    session_code="sess-chat",
    session_context_data=[{"role": "user", "content": "hi"}],
    username="tester",
    version=None,
    channel_type="bkplugin",
)

# 与 agent_bkplugin:449 / views/chat.py:446 / wxaibot/strategies.py:259 同形的 FLOW 构建调用
FLOW_CALL = dict(
    agent_type=AgentType.FLOW,
    build_type=AgentBuildType.DIRECT,
    session_code="sess-flow",
    session_context_data=[],
    username="tester",
    task_id="t-1",
    flow_start_params={"session_code": "sess-flow"},
    poll_interval=1.0,
    poll_timeout=60,
)


def test_create_and_resume_route_through_build_agent(monkeypatch):
    """注入点默认实现只做委派：create / resume 都必须落到 ``build_agent``，不得丢参数。"""
    calls: list[dict] = []

    def fake_build_agent(**kwargs):
        calls.append(kwargs)
        return "built"

    monkeypatch.setattr(AgentInstanceFactory, "build_agent", fake_build_agent)

    assert agent_factory().create(**CHAT_CALL) == "built"
    assert agent_factory().create(**FLOW_CALL) == "built"
    assert len(calls) == 2
    assert calls[0]["build_type"] == AgentBuildType.DIRECT
    assert calls[0]["session_code"] == "sess-chat"
    assert calls[0]["channel_type"] == "bkplugin"
    assert calls[1]["agent_type"] == AgentType.FLOW
    assert calls[1]["task_id"] == "t-1"

    calls.clear()
    assert agent_factory().resume(session_code="sess-1", resume={"input": "继续"}, agent_code="agent-x") == "built"
    (resume_call,) = calls
    assert resume_call["session_code"] == "sess-1"
    assert resume_call["agent_code"] == "agent-x"

    # 形状不支持的恢复负载在委派前就被拦下
    with pytest.raises(AgentFactoryError) as excinfo:
        agent_factory().resume(session_code="sess-1", resume={"foo": "bar"})
    assert excinfo.value.code == "resume_payload_unsupported"


def test_create_forwards_all_declared_params(monkeypatch):
    """5 处调用方只用关键字入参——委派必须全量透传，不得丢参数或改位置传参。"""
    calls: list[dict] = []
    monkeypatch.setattr(AgentInstanceFactory, "build_agent", lambda **kwargs: calls.append(kwargs) or "built")

    agent_factory().create(
        agent_code="agent-x",
        agent_type=AgentType.CHAT,
        build_type=AgentBuildType.SESSION,
        session_code="sess-1",
        temperature=0.2,
        max_tokens=512,
        retry_strategy="sdk",
        switch_agent_by_scene=True,
        is_temporary=True,
        username="tester",
        version="v2",
        hook_handlers="hs",
        extra_flag=True,
    )
    (call,) = calls
    assert call["agent_code"] == "agent-x"
    assert call["max_tokens"] == 512
    assert call["retry_strategy"] == "sdk"
    assert call["switch_agent_by_scene"] is True
    assert call["is_temporary"] is True
    assert call["hook_handlers"] == "hs"
    assert call["extra_flag"] is True


def test_invalid_build_type_and_agent_type_keep_baseline_errors():
    # 非法 build_type：先被 _validate_params 拦下，异常类型与文案与基线一致
    with pytest.raises(ValueError, match="Unsupported build_type"):
        AgentInstanceFactory.build_agent(build_type="unknown", session_code="s")

    # 非法 agent_type（TASK 未注册）：文案里列出已注册类型
    with pytest.raises(ValueError, match="Unsupported agent_type"):
        AgentInstanceFactory.build_agent(agent_type=AgentType.TASK, session_code="s")
