# -*- coding: utf-8 -*-
"""T-11：契约兼容与前端零 diff（REQ-13）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.code_mode.tool import CODE_EXECUTE_TOOL_NAME
from aidev_agent.pydantic_models import AgentExecutorKwargs, ExecuteKwargs, SecuritySettings
from langchain_core.tools import StructuredTool

REPO_ROOT = Path(__file__).resolve().parents[6]
FRONTEND_DIR = "src/frontend/ai-blueking/packages/chat-x"


def _plain_tool():
    def _run(**_: object) -> str:  # noqa: ARG001
        return "ok"

    return StructuredTool.from_function(func=_run, name="calculator", description="calculator tool")


def _assemble(execute_kwargs: ExecuteKwargs):
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(security_settings=SecuritySettings(), execute_kwargs=execute_kwargs)
    )
    tools = builder._prepare_agent_tools(extra_tools=[_plain_tool()], langchain_middleware=[])
    return [tool.name for tool in tools], [entry.name for entry in builder._code_mode_manifest]


@pytest.mark.parametrize(
    "kwargs",
    [
        ExecuteKwargs(tool_use_mode="code_mode", stream=True),
        ExecuteKwargs(tool_use_mode="code_mode", stream=False),
        ExecuteKwargs(tool_use_mode="code_mode", resume={"step": 1}),
    ],
)
def test_three_paths_assemble_same_tool_surface(kwargs):
    tool_names, manifest_names = _assemble(kwargs)
    assert tool_names == [CODE_EXECUTE_TOOL_NAME]
    assert manifest_names == ["calculator"]


def test_resume_without_field_falls_back_to_function_call():
    tool_names, manifest_names = _assemble(ExecuteKwargs(resume={"step": 1}))
    assert CODE_EXECUTE_TOOL_NAME not in tool_names
    assert manifest_names == []


def test_chat_x_has_no_diff():
    try:
        result = subprocess.run(  # noqa: S603
            ["git", "diff", "--name-only", "HEAD", "--", FRONTEND_DIR],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("当前环境没有可用的 git 工作区")

    assert result.stdout.strip() == ""
