# -*- coding: utf-8 -*-
"""code mode 测试的共享假件。

``FakeCodeModeBackend`` 是一个「可编程」的本地后端：真实执行 python、真实读写
文件，同时记录每次 ``execute`` 的入参，用于断言桥的往返契约（ACC-15 的
E2B / paas 以同一契约的 fake 后端取证）。
"""

from __future__ import annotations

import time
from typing import List

import pytest
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode import (
    CodeModeInvoker,
    CodeModeSession,
    build_code_execute_tool,
    build_manifest,
)
from aidev_agent.core.tools.runtime_tools.local_backend import FilesystemBackend
from aidev_agent.core.tools.runtime_tools.provider import RuntimeBackendResolver
from aidev_agent.core.tools.runtime_tools.types import ExecuteResult
from aidev_agent.pydantic_models import SecuritySettings
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool


class FakeCodeModeBackend(FilesystemBackend):
    """记录 execute 入参、可注入延迟 / 失败的本地后端。"""

    def __init__(self, root_dir, *, fail_execute: bool = False, delay: float = 0.0) -> None:
        super().__init__(root_dir=root_dir)
        self.execute_calls: List[dict] = []
        self.fail_execute = fail_execute
        self.delay = delay

    def execute(self, command, timeout=120, max_output_size=100000, *, config=None, state=None):
        self.execute_calls.append({"command": command, "timeout": timeout, "max_output_size": max_output_size})
        if self.delay:
            time.sleep(self.delay)
        if self.fail_execute:
            return ExecuteResult(output="Error: sandbox unavailable", exit_code=None)
        return super().execute(command, timeout, max_output_size, config=config, state=state)


class RecordingTools:
    """一组可断言的假工具：加法、回显与一个总是失败的工具。"""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def _add(self, a: int, b: int) -> str:
        """把两个整数相加。"""
        self.calls.append(("add", a, b))
        return str(a + b)

    def _echo(self, text: str) -> str:
        """原样返回文本。"""
        self.calls.append(("echo", text))
        return text

    def _boom(self, message: str = "boom") -> str:
        """总是失败。"""
        self.calls.append(("boom", message))
        raise RuntimeError(message)

    def tools(self) -> list:
        return [
            StructuredTool.from_function(func=self._add, name="add", description="把两个整数相加。"),
            StructuredTool.from_function(func=self._echo, name="echo", description="原样返回文本。"),
            StructuredTool.from_function(func=self._boom, name="boom", description="总是失败的工具。"),
        ]


def build_resolver(backend, *, default_runtime: str = "local") -> RuntimeBackendResolver:
    resolver = RuntimeBackendResolver(default_runtime=default_runtime, security_settings=SecuritySettings())
    resolver.register_runtime(default_runtime, backend)
    return resolver


def build_tool(
    *,
    backend,
    tools,
    max_retries: int = 3,
    max_tool_calls: int | None = None,
    execute_timeout: int | None = None,
    output_limit: int | None = None,
    event_emitter=None,
    tool_node_options: ToolNodeSettings | None = None,
):
    """按生产装配路径构造 ``code_execute``（内层 ToolNode + invoker + bridge）。"""

    security = SecuritySettings()
    node_options = tool_node_options or ToolNodeSettings(use_timer=False)
    node = build_tool_node(tools, node_options=node_options, security_settings=security)
    invoker = CodeModeInvoker(tools=tools, tool_node=node, event_emitter=event_emitter)
    session = CodeModeSession(max_retries=max_retries)
    tool = build_code_execute_tool(
        session=session,
        resolver=build_resolver(backend),
        invoker=invoker,
        manifest=build_manifest(tools),
        max_tool_calls=max_tool_calls,
        execute_timeout=execute_timeout,
        output_limit=output_limit,
        poll_interval=0.01,
    )
    return tool, session


def make_config() -> RunnableConfig:
    return RunnableConfig(configurable={"thread_id": "code-mode-test"})


@pytest.fixture
def fake_backend(tmp_path):
    return FakeCodeModeBackend(tmp_path)


@pytest.fixture
def failing_backend(tmp_path):
    return FakeCodeModeBackend(tmp_path, fail_execute=True)


@pytest.fixture
def recording_tools():
    return RecordingTools()


@pytest.fixture
def tool_factory():
    """按生产装配路径构造 ``code_execute`` 的工厂。"""

    def _make(backend, tools, **kwargs):
        return build_tool(backend=backend, tools=tools, **kwargs)

    return _make


@pytest.fixture
def code_tool(fake_backend, recording_tools):
    tool, session = build_tool(backend=fake_backend, tools=recording_tools.tools())
    return tool, session
