# -*- coding: utf-8 -*-
"""T-5：宿主侧桥执行器（REQ-4 / REQ-6 / REQ-9）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode import CodeModeBridge
from aidev_agent.core.tools.code_mode.errors import STATUS_INFRA_ERROR, STATUS_OK
from aidev_agent.core.tools.code_mode.invoker import CodeModeInvoker
from aidev_agent.core.tools.code_mode.manifest import build_manifest
from aidev_agent.pydantic_models import SecuritySettings


def _bridge(backend, tools, **kwargs):
    node = build_tool_node(tools, node_options=ToolNodeSettings(use_timer=False), security_settings=SecuritySettings())
    invoker = CodeModeInvoker(tools=tools, tool_node=node)
    return CodeModeBridge(
        backend=backend,
        invoker=invoker,
        manifest=build_manifest(tools),
        poll_interval=0.01,
        **kwargs,
    )


@pytest.mark.asyncio
async def test_two_calls_round_trip_in_order(fake_backend, recording_tools):
    tools = recording_tools.tools()
    result = await _bridge(fake_backend, tools).arun(
        """
first = add(a=1, b=2)
second = echo(text="hello")
print(first, second)
"""
    )

    assert result.status == STATUS_OK
    assert recording_tools.calls[:2] == [("add", 1, 2), ("echo", "hello")]
    assert "3 hello" in result.output
    # 内层调用记录（工具名 / 入参）不得作为输出的一部分回填上下文
    assert "add(" not in result.output
    assert "cm-" not in result.output


@pytest.mark.asyncio
async def test_execute_uses_configured_timeout_and_output_limit(fake_backend, recording_tools):
    bridge = _bridge(fake_backend, recording_tools.tools(), execute_timeout=7, output_limit=11)
    await bridge.arun('print("hello world")')

    call = fake_backend.execute_calls[0]
    assert call["timeout"] == 7
    assert call["max_output_size"] == 11
    assert ".aidev_code_mode/" in call["command"]


@pytest.mark.asyncio
async def test_output_over_limit_is_truncated_with_marker(fake_backend, recording_tools):
    result = await _bridge(fake_backend, recording_tools.tools(), output_limit=10).arun('print("x" * 100)')

    assert result.status == STATUS_OK
    assert result.output.endswith("[Output was truncated due to size limits]")


@pytest.mark.asyncio
async def test_backend_failure_returns_infra_error(failing_backend, recording_tools):
    result = await _bridge(failing_backend, recording_tools.tools()).arun('print("hi")')

    assert result.status == STATUS_INFRA_ERROR
    assert "[code_mode] status=infra_error" in result.output
    assert recording_tools.calls == []
