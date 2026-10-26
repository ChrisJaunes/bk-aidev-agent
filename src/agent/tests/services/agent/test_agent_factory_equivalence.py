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
from aidev_agent.services.agent.factory import AgentInstanceFactory, DefaultAgentFactory, agent_factory


class _RecordingFactory(DefaultAgentFactory):
    """记录 create / resume 调用的替换实现（不真正装配）。"""

    def __init__(self):
        self.create_calls: list[dict] = []
        self.resume_calls: list[dict] = []

    def create(self, **build_params):
        self.create_calls.append(build_params)
        return "created"

    def resume(self, *, session_code, resume, **build_params):
        self.resume_calls.append({"session_code": session_code, "resume": resume, **build_params})
        return "resumed"


@pytest.fixture
def recording():
    factory = _RecordingFactory()
    legacy = agent_factory.replace_defaults(factory)
    try:
        yield factory
    finally:
        agent_factory.replace_defaults(legacy)


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


def test_build_agent_delegates_every_call_site_shape_to_create(recording):
    assert AgentInstanceFactory.build_agent(**CHAT_CALL) == "created"
    assert AgentInstanceFactory.build_agent(**FLOW_CALL) == "created"

    assert len(recording.create_calls) == 2
    assert recording.resume_calls == []
    assert recording.create_calls[0]["build_type"] == AgentBuildType.DIRECT
    assert recording.create_calls[0]["session_code"] == "sess-chat"
    assert recording.create_calls[0]["channel_type"] == "bkplugin"
    assert recording.create_calls[1]["agent_type"] == AgentType.FLOW
    assert recording.create_calls[1]["task_id"] == "t-1"


def test_build_agent_forwards_all_declared_params(recording):
    """5 处调用方只用关键字入参——委派必须全量透传，不得丢参数或改位置传参。"""
    AgentInstanceFactory.build_agent(
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
        extra_flag=True,
    )
    (call,) = recording.create_calls
    assert call["agent_code"] == "agent-x"
    assert call["max_tokens"] == 512
    assert call["retry_strategy"] == "sdk"
    assert call["switch_agent_by_scene"] is True
    assert call["is_temporary"] is True
    assert call["extra_flag"] is True


def test_invalid_build_type_and_agent_type_keep_baseline_errors():
    # 非法 build_type：先被 _validate_params 拦下，异常类型与文案与基线一致
    with pytest.raises(ValueError, match="Unsupported build_type"):
        AgentInstanceFactory.build_agent(build_type="unknown", session_code="s")

    # 非法 agent_type（TASK 未注册）：文案里列出已注册类型
    with pytest.raises(ValueError, match="Unsupported agent_type"):
        AgentInstanceFactory.build_agent(agent_type=AgentType.TASK, session_code="s")
