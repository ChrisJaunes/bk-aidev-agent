# -*- coding: utf-8 -*-
"""T-7：``code_execute`` 工具与内层调用事件（REQ-3 / REQ-7）。

内层事件复用既有 ``manually_emit_tool_call`` 事件：由 AG-UI 适配器转成一对
``TOOL_CALL_START/ARGS/END``，不新增事件类型、不改字段语义（ACC-11 / ACC-12）。
"""

from __future__ import annotations

import json

import pytest
from ag_ui.core import EventType
from aidev_agent.core.ag_ui.agent import LangGraphAgent
from aidev_agent.core.ag_ui.types import CustomEventNames
from aidev_agent.core.graphs.react import code_mode as code_mode_module
from aidev_agent.core.graphs.react.code_mode import emit_tool_call
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode import (
    CodeModeInvoker,
    CodeModeSession,
    build_code_execute_tool,
    build_manifest,
)
from aidev_agent.core.tools.code_mode.tool import CODE_EXECUTE_TOOL_NAME
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.provider import RuntimeBackendResolver
from aidev_agent.pydantic_models import SecuritySettings
from langchain_core.tools import StructuredTool

INNER_TRIPLET = (EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END)
BASELINE_TYPES = frozenset({*INNER_TRIPLET, EventType.CUSTOM})


def _dummy_agent() -> LangGraphAgent:
    agent = object.__new__(LangGraphAgent)
    agent.front_end_display = True
    return agent


async def _adapt(events: list[dict]) -> list:
    agent = _dummy_agent()
    adapted = []
    for event in events:
        async for item in LangGraphAgent._handle_on_custom_event(agent, event):
            adapted.append(item)
    return adapted


@pytest.fixture
def captured_events(monkeypatch):
    captured: list[dict] = []

    def _capture(name, data=None, config=None):  # noqa: ARG001
        captured.append({"name": name, "data": data})

    monkeypatch.setattr(code_mode_module, "dispatch_custom_event", _capture)
    return captured


def test_emit_tool_call_uses_existing_event_name(captured_events):
    emit_tool_call(tool_name="read_file", args={"file_path": "/tmp/a"}, call_id="cm-abc-0001", config=None)

    assert len(captured_events) == 1
    event = captured_events[0]
    assert event["name"] == CustomEventNames.ManuallyEmitToolCall.value
    assert event["data"]["id"] == "cm-abc-0001"
    assert event["data"]["name"] == "read_file"
    assert json.loads(event["data"]["args"]) == {"file_path": "/tmp/a"}


@pytest.mark.asyncio
async def test_adapter_turns_custom_event_into_triplet(captured_events):
    emit_tool_call(tool_name="read_file", args={"file_path": "/tmp/a"}, call_id="cm-abc-0001", config=None)

    events = await _adapt(captured_events)

    types = [event.type for event in events]
    assert tuple(item for item in types if item != EventType.CUSTOM) == INNER_TRIPLET
    assert set(types) <= BASELINE_TYPES
    assert {event.tool_call_id for event in events if hasattr(event, "tool_call_id")} == {"cm-abc-0001"}
    assert events[1].delta == json.dumps({"file_path": "/tmp/a"})


@pytest.mark.asyncio
async def test_two_inner_calls_produce_two_distinct_triplets(tmp_path, captured_events):
    def _add(a: int, b: int) -> str:
        """把两个整数相加。"""
        return str(a + b)

    def _echo(text: str) -> str:
        """原样返回文本。"""
        return text

    tools = [
        StructuredTool.from_function(func=_add, name="add", description="把两个整数相加。"),
        StructuredTool.from_function(func=_echo, name="echo", description="原样返回文本。"),
    ]
    resolver = RuntimeBackendResolver(default_runtime="local", security_settings=SecuritySettings())
    resolver.register_runtime("local", FilesystemBackend(root_dir=tmp_path))
    invoker = CodeModeInvoker(
        tools=tools,
        tool_node=build_tool_node(
            tools, node_options=ToolNodeSettings(use_timer=False), security_settings=SecuritySettings()
        ),
        event_emitter=emit_tool_call,
    )
    tool = build_code_execute_tool(
        session=CodeModeSession(),
        resolver=resolver,
        invoker=invoker,
        manifest=build_manifest(tools),
        poll_interval=0.01,
    )
    code = 'print(add(a=1, b=2)); print(echo(text="x"))'

    await tool.ainvoke({"code": code}, config={"configurable": {"thread_id": "t"}})

    assert len(captured_events) == 2
    ids = [event["data"]["id"] for event in captured_events]
    assert len(set(ids)) == 2
    assert all(call_id != CODE_EXECUTE_TOOL_NAME for call_id in ids)

    events = await _adapt(captured_events)
    types = [event.type for event in events]
    assert tuple(item for item in types if item != EventType.CUSTOM) == INNER_TRIPLET * 2
    assert set(types) <= BASELINE_TYPES
    assert [event.tool_call_name for event in events if event.type == EventType.TOOL_CALL_START] == ["add", "echo"]
