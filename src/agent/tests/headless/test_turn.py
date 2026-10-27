# -*- coding: utf-8 -*-
"""T-6：``create`` / ``turn`` 与会话驱动（REQ-3 / REQ-6 / REQ-7 / REQ-9）。"""

import pytest
from ag_ui.core import EventType
from aidev_agent.enums import AgentBuildType
from aidev_agent.services.agent.factory import AgentInstanceFactory, DefaultAgentFactory, agent_factory
from langgraph.checkpoint.memory import MemorySaver

from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import EventSink, Harness

BASELINE_EVENT_TYPES = {member.value for member in EventType}


class _RecordingFactory(DefaultAgentFactory):
    def __init__(self):
        self.calls: list[dict] = []

    def create(self, **build_params):
        self.calls.append(build_params)
        return super().create(**build_params)


@pytest.fixture
def llm():
    with FakeLLMServer(["你好呀"]) as server:
        yield server


@pytest.fixture
def recording_factory():
    factory = _RecordingFactory()
    legacy = agent_factory.replace_defaults(factory)
    try:
        yield factory
    finally:
        agent_factory.replace_defaults(legacy)


def _create(harness: Harness, llm: FakeLLMServer, *, session_code="s1", **extra):
    params = {
        "session_code": session_code,
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": llm.base_url, "model": "fake-model"},
    }
    params.update(extra)
    harness.send_obj("c1", "create", params)
    return harness.result("c1")


def test_create_goes_through_injected_factory_and_echoes_session_code(llm, recording_factory):
    harness = Harness()
    result = _create(harness, llm)
    assert result == {"session_code": "s1", "created": True}
    assert recording_factory.calls, "create 必须经注入点 agent_factory().create"
    assert recording_factory.calls[0]["session_code"] == "s1"


def test_turn_event_sequence_is_baseline_agui_subset(llm):
    harness = Harness()
    _create(harness, llm)
    harness.send("t1", "turn", session_code="s1", input="你好")
    harness.wait()

    types = harness.event_types("t1")
    assert types[0] == EventType.RUN_STARTED.value
    assert types[-1] == EventType.RUN_FINISHED.value
    assert EventType.TEXT_MESSAGE_CONTENT.value in types
    assert set(types) <= BASELINE_EVENT_TYPES, set(types) - BASELINE_EVENT_TYPES

    result = harness.result("t1")
    assert result["session_code"] == "s1"
    assert result["status"] == "finished"


def test_turn_writes_user_and_assistant_into_ledger(llm):
    harness = Harness()
    _create(harness, llm)
    harness.send("t1", "turn", session_code="s1", input="你好")
    harness.wait()

    ledger = harness.dispatcher.registry.get("s1").ledger.records()
    assert [record["role"] for record in ledger] == ["user", "assistant"]
    assert ledger[0]["content"] == "你好"
    assert ledger[1]["content"] == "你好呀"


def test_turn_with_messages_uses_direct_build_and_skips_ledger(llm, recording_factory):
    harness = Harness()
    _create(harness, llm)
    recording_factory.calls.clear()

    history = [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]
    harness.send("t1", "turn", session_code="s1", input="继续", messages=history)
    harness.wait()

    assert harness.result("t1")["status"] == "finished"
    (call,) = [item for item in recording_factory.calls if item.get("session_code") == "s1"]
    assert call["build_type"] == AgentBuildType.DIRECT
    assert call["session_context_data"] == history

    # 无状态模式不写账本
    assert harness.dispatcher.registry.get("s1").ledger.records() == []
    # fake 模型收到的请求体包含该历史
    bodies = " ".join(str(body) for body in llm.requests)
    assert "上一轮问题" in bodies and "上一轮回答" in bodies


def test_custom_event_sink_keeps_ledger_complete(llm):
    sink = EventSink()
    harness = Harness()
    _create(harness, llm, event_sink=sink)
    harness.send("t1", "turn", session_code="s1", input="你好")
    harness.wait()

    # 事件交给 sink，stdout 不再有事件行（只有响应行）
    assert harness.events("t1") == []
    assert sink.types()[0] == EventType.RUN_STARTED.value
    assert sink.types()[-1] == EventType.RUN_FINISHED.value

    ledger = harness.dispatcher.registry.get("s1").ledger.records()
    assert [record["role"] for record in ledger] == ["user", "assistant"]


def test_default_path_and_build_agent_share_the_same_registry(llm):
    harness = Harness()
    _create(harness, llm)
    # create 走注册中心；build_agent 仍可正常调用（签名与默认值不变）
    agent = AgentInstanceFactory.build_agent(
        agent_code="agent-hook",
        build_type=AgentBuildType.DIRECT,
        session_context_data=[{"role": "user", "content": "hi"}],
        resource_manager=harness.dispatcher.registry.get("s1").resource_manager,
        checkpointer=MemorySaver(),
    )
    assert hasattr(agent, "execute") and hasattr(agent, "stop")
