# -*- coding: utf-8 -*-
"""T-1：``tool_use_mode`` 开关与限额配置（REQ-1 / REQ-2 / REQ-13）。"""

from __future__ import annotations

import os
from typing import List

import pytest
from aidev_agent.config import settings
from aidev_agent.core.graphs.react.code_mode import TOOL_USE_MODE_CODE_MODE, resolve_tool_use_mode
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.code_mode.tool import CODE_EXECUTE_TOOL_NAME
from aidev_agent.pydantic_models import AgentExecutorKwargs, ExecuteKwargs, SecuritySettings
from langchain_core.tools import BaseTool, StructuredTool
from pydantic import ValidationError


def _plain_tool(name: str = "calculator") -> BaseTool:
    def _run(**_: object) -> str:  # noqa: ARG001
        return "ok"

    return StructuredTool.from_function(func=_run, name=name, description=f"{name} tool")


def _assemble(execute_kwargs: ExecuteKwargs, extra: List[BaseTool]) -> List[BaseTool]:
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(security_settings=SecuritySettings(), execute_kwargs=execute_kwargs)
    )
    return builder._prepare_agent_tools(extra_tools=extra, langchain_middleware=[])


class TestToolUseModeField:
    def test_default_is_function_call(self):
        assert ExecuteKwargs().tool_use_mode == "function_call"

    @pytest.mark.parametrize("value", ["function_call", "code_mode"])
    def test_accepts_legal_values(self, value):
        assert ExecuteKwargs(tool_use_mode=value).tool_use_mode == value

    def test_rejects_unknown_value(self):
        with pytest.raises(ValidationError):
            ExecuteKwargs(tool_use_mode="code_mode_v2")

    def test_model_construct_bypass_is_rejected_at_assembly(self):
        kwargs = ExecuteKwargs.model_construct(tool_use_mode="whatever")
        with pytest.raises(ValueError, match="tool_use_mode"):
            resolve_tool_use_mode(kwargs)


class TestDefaultPathUnchanged:
    def test_default_keeps_baseline_tool_set(self):
        extra = [_plain_tool()]
        without_field = _assemble(ExecuteKwargs(), extra)
        explicit = _assemble(ExecuteKwargs(tool_use_mode="function_call"), extra)

        assert [tool.name for tool in without_field] == [tool.name for tool in explicit]
        assert CODE_EXECUTE_TOOL_NAME not in {tool.name for tool in without_field}

    def test_code_mode_binds_single_tool(self):
        tools = _assemble(ExecuteKwargs(tool_use_mode=TOOL_USE_MODE_CODE_MODE), [_plain_tool()])
        assert [tool.name for tool in tools] == [CODE_EXECUTE_TOOL_NAME]


@pytest.mark.parametrize(
    "name, expected",
    [
        ("CODE_MODE_EXECUTE_TIMEOUT", 120),
        ("CODE_MODE_MAX_TOOL_CALLS", 50),
        ("CODE_MODE_OUTPUT_LIMIT", 100000),
        ("CODE_MODE_MAX_RETRIES", 3),
        ("CODE_MODE_BRIDGE_POLL_INTERVAL", 0.1),
    ],
)
def test_code_mode_settings_defaults(name, expected):
    if name in os.environ:
        pytest.skip(f"{name} 被环境变量覆盖")
    assert settings.get(name) == expected
