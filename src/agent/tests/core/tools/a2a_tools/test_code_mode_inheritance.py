# -*- coding: utf-8 -*-
"""T-10：A2A 子 Agent 继承父 Agent 的 ``tool_use_mode``（REQ-11）。"""

from __future__ import annotations

import pytest
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.a2a_tools.utils import extract_child_execute_kwargs
from aidev_agent.core.tools.code_mode.tool import CODE_EXECUTE_TOOL_NAME
from aidev_agent.pydantic_models import AgentExecutorKwargs, ExecuteKwargs, SecuritySettings
from langchain_core.runnables import RunnableConfig


def _child_kwargs(tool_use_mode: str) -> ExecuteKwargs:
    parent = ExecuteKwargs(tool_use_mode=tool_use_mode, session_code="parent-session")
    config = RunnableConfig(configurable={"execute_kwargs": parent})
    return extract_child_execute_kwargs(config, session_code="child-session")


@pytest.mark.parametrize("tool_use_mode", ["code_mode", "function_call"])
def test_child_inherits_tool_use_mode(tool_use_mode):
    child = _child_kwargs(tool_use_mode)
    assert child.tool_use_mode == tool_use_mode
    assert child.session_code == "child-session"


def test_child_code_mode_binds_single_code_execute():
    child = _child_kwargs("code_mode")
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(security_settings=SecuritySettings(), execute_kwargs=child)
    )

    tools = builder._prepare_agent_tools(extra_tools=[], langchain_middleware=[])

    assert [tool.name for tool in tools] == [CODE_EXECUTE_TOOL_NAME]
