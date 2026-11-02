# -*- coding: utf-8 -*-
"""T-8：异常分类、超时与重试终止（REQ-8）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.tools.code_mode.errors import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_RUNTIME_ERROR,
    STATUS_SYNTAX_ERROR,
    STATUS_TIMEOUT,
    STATUS_TOOL_ERROR,
    CodeModeRetryExhausted,
)
from aidev_agent.core.tools.code_mode.tool import build_code_execute_tool
from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp


async def _run(tool, code: str) -> str:
    return await tool.ainvoke({"code": code}, config=RunnableConfig(configurable={"thread_id": "t"}))


@pytest.mark.asyncio
async def test_syntax_error_is_returned(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools())
    output = await _run(tool, "def broken(:\n    pass")
    assert f"[code_mode] status={STATUS_SYNTAX_ERROR}" in output
    assert recording_tools.calls == []


@pytest.mark.asyncio
async def test_runtime_error_is_returned(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools())
    output = await _run(tool, "raise ValueError('bad math')")
    assert f"[code_mode] status={STATUS_RUNTIME_ERROR}" in output
    assert "bad math" in output


@pytest.mark.asyncio
async def test_tool_error_can_be_caught_by_model_code(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools())
    output = await _run(
        tool,
        """
try:
    boom(message="expected")
except Exception as exc:
    print("caught:", exc)
""",
    )
    assert "caught:" in output
    assert "expected" in output
    assert "[code_mode] status=" not in output


@pytest.mark.asyncio
async def test_uncaught_tool_error_is_reported(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools())
    output = await _run(tool, 'boom(message="unhandled")')
    assert f"[code_mode] status={STATUS_TOOL_ERROR}" in output


@pytest.mark.asyncio
async def test_timeout_is_not_success(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools(), execute_timeout=1)
    output = await _run(tool, "import time\ntime.sleep(10)")
    assert f"[code_mode] status={STATUS_TIMEOUT}" in output


@pytest.mark.asyncio
async def test_budget_exceeded(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools(), max_tool_calls=1)
    output = await _run(tool, "add(a=1, b=2)\nadd(a=2, b=3)")
    assert f"[code_mode] status={STATUS_BUDGET_EXCEEDED}" in output


@pytest.mark.asyncio
async def test_retry_exhaustion_raises_after_configured_retries(fake_backend, recording_tools, tool_factory):
    tool, session = tool_factory(fake_backend, recording_tools.tools(), max_retries=2)

    for _ in range(2):
        output = await _run(tool, "raise RuntimeError('still broken')")
        assert f"[code_mode] status={STATUS_RUNTIME_ERROR}" in output

    with pytest.raises(CodeModeRetryExhausted):
        await _run(tool, "raise RuntimeError('still broken')")

    assert session.retries_used == 3
    assert len(fake_backend.execute_calls) == 3
    assert issubclass(CodeModeRetryExhausted, GraphBubbleUp)


@pytest.mark.asyncio
async def test_success_resets_failure_counter(fake_backend, recording_tools, tool_factory):
    tool, session = tool_factory(fake_backend, recording_tools.tools(), max_retries=1)

    await _run(tool, "raise RuntimeError('once')")
    await _run(tool, "print('recovered')")
    assert session.retries_used == 0

    await _run(tool, "raise RuntimeError('twice')")
    assert session.retries_used == 1


def test_build_code_execute_tool_has_no_approval_metadata(fake_backend, recording_tools, tool_factory):
    tool, _ = tool_factory(fake_backend, recording_tools.tools())
    assert build_code_execute_tool is not None
    assert tool.name == "code_execute"
    assert not (tool.metadata or {}).get("approval")
