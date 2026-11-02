# -*- coding: utf-8 -*-
"""T-2：装配期工具面与过滤（REQ-2 / REQ-3 / REQ-5）。"""

from __future__ import annotations

from typing import List

import pytest
from aidev_agent.core.graphs.react.graph import ReActAgentBuilder
from aidev_agent.core.tools.code_mode.tool import CODE_EXECUTE_TOOL_NAME
from aidev_agent.packages.interrupt_manager import ASK_USER_QUESTION_TOOL_NAME
from aidev_agent.pydantic_models import (
    AgentExecutorKwargs,
    ExecuteKwargs,
    SecurityCommandSettings,
    SecuritySettings,
)
from langchain_core.tools import BaseTool, StructuredTool

APPROVAL_TOOL_NAME = "itsm_tool"
PLAIN_TOOL_NAME = "calculator"


def _plain_tool(name: str = PLAIN_TOOL_NAME, **kwargs) -> BaseTool:
    def _run(**_: object) -> str:  # noqa: ARG001
        return "ok"

    return StructuredTool.from_function(func=_run, name=name, description=f"{name} tool", **kwargs)


def _approval_tool() -> BaseTool:
    return _plain_tool(APPROVAL_TOOL_NAME, metadata={"approval": {"enabled": True}})


def _builder_code_mode(**kwargs) -> ReActAgentBuilder:
    return ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(
            security_settings=SecuritySettings(),
            execute_kwargs=ExecuteKwargs(tool_use_mode="code_mode", **kwargs),
        )
    )


def _assemble(builder: ReActAgentBuilder, extra: List[BaseTool]) -> List[BaseTool]:
    return builder._prepare_agent_tools(extra_tools=extra, langchain_middleware=[])


@pytest.fixture
def all_tools() -> List[BaseTool]:
    # ask 工具由 builder 自动注入（避免重名去重把 ask 改名，掩盖名字口径）
    return [_approval_tool(), _plain_tool()]


def test_code_mode_returns_only_code_execute(all_tools):
    tools = _assemble(_builder_code_mode(), all_tools)
    assert [tool.name for tool in tools] == [CODE_EXECUTE_TOOL_NAME]
    assert not (tools[0].metadata or {}).get("approval")


def test_inner_set_excludes_ask_and_approval(all_tools):
    builder = _builder_code_mode()
    _assemble(builder, all_tools)

    names = {entry.name for entry in builder._code_mode_manifest}
    assert names == {PLAIN_TOOL_NAME}


def test_prompt_matches_inner_set(all_tools):
    builder = _builder_code_mode()
    _assemble(builder, all_tools)

    assert builder._code_mode_prompt
    for entry in builder._code_mode_manifest:
        assert entry.name in builder._code_mode_prompt
    assert ASK_USER_QUESTION_TOOL_NAME not in builder._code_mode_prompt
    assert APPROVAL_TOOL_NAME not in builder._code_mode_prompt


@pytest.mark.parametrize("mode", ["default", "dontAsk", "dontInterrupt"])
def test_code_mode_still_excludes_interrupt_tools_for_every_mode(all_tools, mode):
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(
            security_settings=SecuritySettings(),
            execute_kwargs=ExecuteKwargs(tool_use_mode="code_mode", mode=mode),
        )
    )
    _assemble(builder, all_tools)

    names = {entry.name for entry in builder._code_mode_manifest}
    assert names == {PLAIN_TOOL_NAME}


def test_tool_deny_removes_inner_tool(all_tools):
    builder = _builder_code_mode(tool_deny=[PLAIN_TOOL_NAME])
    _assemble(builder, all_tools)
    assert builder._code_mode_manifest == []


def test_tool_allow_restricts_inner_tool(all_tools):
    builder = _builder_code_mode(tool_allow=[PLAIN_TOOL_NAME, CODE_EXECUTE_TOOL_NAME])
    tools = _assemble(builder, all_tools)

    assert [entry.name for entry in builder._code_mode_manifest] == [PLAIN_TOOL_NAME]
    assert [tool.name for tool in tools] == [CODE_EXECUTE_TOOL_NAME]


def test_code_execute_can_be_denied(all_tools):
    builder = _builder_code_mode(tool_deny=[CODE_EXECUTE_TOOL_NAME])
    assert _assemble(builder, all_tools) == []


def test_function_call_mode_keeps_baseline_tools(all_tools):
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(security_settings=SecuritySettings(), execute_kwargs=ExecuteKwargs())
    )
    names = {tool.name for tool in _assemble(builder, all_tools)}

    assert names == {ASK_USER_QUESTION_TOOL_NAME, APPROVAL_TOOL_NAME, PLAIN_TOOL_NAME}
    assert CODE_EXECUTE_TOOL_NAME not in names
    assert builder._code_mode_prompt is None


@pytest.mark.parametrize("mode", ["default", "dontAsk", "dontInterrupt"])
def test_code_mode_disables_command_approval(mode):
    """REQ-5：code mode 下命令级审批不得发起（灰名单 fail-closed）。"""

    injected = SecuritySettings(
        command=SecurityCommandSettings(command_review_disposition="approval", command_approval_approvers="u1")
    )
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(
            security_settings=injected,
            execute_kwargs=ExecuteKwargs(tool_use_mode="code_mode", mode=mode),
        )
    )

    builder._prepare_agent_options()

    assert injected.command.command_review_disposition == "approval"  # 注入对象不被原地改写
    assert builder._security_settings.command.command_review_disposition == "block"


def test_function_call_keeps_command_approval():
    injected = SecuritySettings(
        command=SecurityCommandSettings(command_review_disposition="approval", command_approval_approvers="u1")
    )
    builder = ReActAgentBuilder().set_bkai_options(
        AgentExecutorKwargs(security_settings=injected, execute_kwargs=ExecuteKwargs())
    )

    builder._prepare_agent_options()

    assert builder._security_settings.command.command_review_disposition == "approval"
