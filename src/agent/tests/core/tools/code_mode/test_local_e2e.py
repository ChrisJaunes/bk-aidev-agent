# -*- coding: utf-8 -*-
"""T-13：``local`` / ``bubblewrap`` 真实后端的端到端（REQ-2 / REQ-4 / REQ-9）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode import CodeModeBridge, CodeModeInvoker, build_manifest
from aidev_agent.core.tools.code_mode.errors import STATUS_OK
from aidev_agent.core.tools.runtime_tools.bubblewrap_backend import BubblewrapFilesystemBackend
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.types import RuntimeBackend
from aidev_agent.pydantic_models import SecuritySettings

CODE = """
items = [1, 2, 3, 4]
total = 0
for item in items:
    if item % 2 == 0:
        total += int(add(a=total, b=item))
    else:
        echo(text=str(item))
print("total=", total)
"""


@pytest.fixture
def tools(recording_tools):
    return recording_tools.tools()


async def _run(backend: RuntimeBackend, tools) -> tuple[str, str]:
    invoker = CodeModeInvoker(
        tools=tools,
        tool_node=build_tool_node(
            tools, node_options=ToolNodeSettings(use_timer=False), security_settings=SecuritySettings()
        ),
    )
    bridge = CodeModeBridge(backend=backend, invoker=invoker, manifest=build_manifest(tools), poll_interval=0.01)
    result = await bridge.arun(CODE)
    return result.status, result.output


@pytest.mark.asyncio
async def test_local_backend_executes_branch_and_aggregation(tmp_path, tools, recording_tools):
    status, output = await _run(FilesystemBackend(root_dir=tmp_path), tools)

    assert status == STATUS_OK
    assert "total= 8" in output
    assert recording_tools.calls == [("echo", "1"), ("add", 0, 2), ("echo", "3"), ("add", 2, 4)]


@pytest.mark.asyncio
async def test_bubblewrap_backend_executes_when_available(tmp_path, tools, recording_tools):
    backend = BubblewrapFilesystemBackend(root_dir=tmp_path)
    if not backend.is_available():
        pytest.skip("当前环境没有可用的 bwrap")

    status, output = await _run(backend, tools)

    assert status == STATUS_OK
    assert "total= 8" in output
