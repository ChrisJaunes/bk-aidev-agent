# -*- coding: utf-8 -*-
"""T-6：内层工具执行器复用既有 wrapper 链（REQ-4 / REQ-10）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.nodes.tool import build_tool_node
from aidev_agent.core.nodes.tool.pydantic_models import ToolNodeSettings
from aidev_agent.core.tools.code_mode.invoker import CodeModeInvoker
from aidev_agent.pydantic_models import SecurityRedactionSettings, SecuritySettings
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool


def _secret_tool() -> StructuredTool:
    def _read() -> str:
        """返回一段含凭据的文本。"""
        return "token=SECRET1234"

    return StructuredTool.from_function(func=_read, name="read_secret", description="读取凭据。")


def _invoker(tools, *, node_options=None, security=None):
    node = build_tool_node(
        tools,
        node_options=node_options or ToolNodeSettings(use_timer=False),
        security_settings=security or SecuritySettings(),
    )
    return CodeModeInvoker(tools=tools, tool_node=node)


class _CountingHandler(BaseCallbackHandler):
    def __init__(self) -> None:
        self.tool_starts = 0

    def on_tool_start(self, *args, **kwargs) -> None:  # noqa: ARG002
        self.tool_starts += 1


@pytest.mark.asyncio
async def test_unknown_tool_is_rejected_without_executing(recording_tools):
    tools = recording_tools.tools()
    invoker = _invoker(tools)

    content, error = await invoker.ainvoke("not_installed", {}, call_id="cm-1")

    assert content is None
    assert "unknown tool" in error
    assert recording_tools.calls == []


@pytest.mark.asyncio
async def test_tool_exception_is_returned_as_error(recording_tools):
    tools = recording_tools.tools()
    invoker = _invoker(tools)

    content, error = await invoker.ainvoke("boom", {"message": "kaboom"}, call_id="cm-2")

    assert content is None
    assert "kaboom" in error
    assert recording_tools.calls == [("boom", "kaboom")]


@pytest.mark.asyncio
async def test_result_limit_wrapper_is_applied():
    def _long() -> str:
        """返回超长文本。"""
        return "x" * 500

    tool = StructuredTool.from_function(func=_long, name="long_tool", description="超长文本。")
    invoker = _invoker([tool], node_options=ToolNodeSettings(use_timer=False, result_limit_thrd=10))

    content, error = await invoker.ainvoke("long_tool", {}, call_id="cm-3")

    assert content is None
    assert "超长" in error


@pytest.mark.asyncio
async def test_redaction_wrapper_is_applied():
    security = SecuritySettings(
        redaction=SecurityRedactionSettings(known_sensitive_values="SECRET1234", enable_redact_secrets=True)
    )
    invoker = _invoker([_secret_tool()], security=security)

    content, error = await invoker.ainvoke("read_secret", {}, call_id="cm-4")

    assert error is None
    assert "SECRET1234" not in content


@pytest.mark.asyncio
async def test_inner_call_does_not_leak_tool_callbacks(recording_tools):
    handler = _CountingHandler()
    config = RunnableConfig(configurable={"thread_id": "t"}, callbacks=[handler])
    invoker = _invoker(recording_tools.tools())

    await invoker.ainvoke("add", {"a": 1, "b": 2}, call_id="cm-5", config=config)

    assert handler.tool_starts == 0
