# -*- coding: utf-8 -*-
"""MCP 工具重名改名后的审批匹配与展示名。

覆盖：
- ``_match_approval_item`` 对改名工具按 ``metadata["original_name"]`` 命中审批配置；
- ``_apply_tool_approval_settings`` 复用调用方传入的审批绑定，``target.display_name`` 为原始名；
- ``build_tools`` 把同一套审批判定作为 ``approval_matcher`` 传给 ``construct_mcp``。
"""

from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.tools import StructuredTool

from aidev_agent.packages.resource_manager.agent import AgentResourceManager
from aidev_agent.services.agent.chat import ChatAgentBuilder

DUP_MCP_CONFIG = {
    "msdk-console-prod-mcp": {"url": "http://msdk1.example.com/mcp", "transport": "streamable_http"},
    "msdk-console-prod-mcp2": {"url": "http://msdk2.example.com/mcp", "transport": "streamable_http"},
}


def _approval_settings(mcp_name: str = "msdk-console-prod-mcp2", tool_name: str = "get_task_list") -> dict:
    return {
        "strategies": [
            {"strategy_id": 7, "approval_name": "任务清单审批", "approvers": ["admin"]},
        ],
        "bindings": [
            {
                "resource_type": "mcp_tool",
                "approval_enabled": True,
                "approval_strategy_id": 7,
                "mcp_id": 11,
                "mcp_name": mcp_name,
                "mcp_tool_name": tool_name,
            }
        ],
    }


def _builder(approval_settings: dict | None = None, resource_manager=None) -> ChatAgentBuilder:
    ctx = MagicMock()
    ctx.session_context_data = []
    ctx.agent_config.approval_settings = approval_settings or {}
    ctx.agent_config.resources = []
    ctx.agent_config.mcp_server_config = DUP_MCP_CONFIG
    ctx.agent_config.tool_codes = []
    ctx.agent_config.tool_ssl_verify = True
    if resource_manager is not None:
        ctx.resource_manager = resource_manager
    return ChatAgentBuilder(ctx)


def _renamed_tool_metadata() -> dict:
    return {
        "mcp_name": "msdk-console-prod-mcp2",
        "mcp_transport": "streamable_http",
        "original_name": "get_task_list",
        "display_name": "get_task_list",
    }


def _make_tool(name: str, metadata: dict) -> MagicMock:
    tool = MagicMock(spec=StructuredTool)
    tool.name = name
    tool.metadata = metadata
    return tool


class TestMatchApprovalItemWithOriginalName:
    def test_matches_by_original_name_after_rename(self):
        builder = _builder(_approval_settings())
        approval_items = builder._normalize_tool_approval_bindings()

        matched = builder._match_approval_item(
            _renamed_tool_metadata(), "msdk-console-prod-mcp2__get_task_list", approval_items
        )

        assert matched is not None
        assert matched["tool_name"] == "get_task_list"

    def test_without_original_name_does_not_match_model_name(self):
        """未改名工具（无 original_name）不会被同服务另一端点的别名误命中。"""
        builder = _builder(_approval_settings(mcp_name="bar-mcp"))
        approval_items = builder._normalize_tool_approval_bindings()
        metadata = {"mcp_name": "bar-mcp"}

        matched = builder._match_approval_item(metadata, "bar-mcp__other_tool", approval_items)

        assert matched is None


class TestApplyApprovalSettingsDisplayName:
    def test_display_name_uses_original_name(self):
        builder = _builder(_approval_settings())
        approval_items = builder._normalize_tool_approval_bindings()
        tool = _make_tool("msdk-console-prod-mcp2__get_task_list", _renamed_tool_metadata())
        # _apply_tool_approval_settings 必须复用外部传入的绑定，不得自行再归一化一次
        builder._normalize_tool_approval_bindings = MagicMock(side_effect=AssertionError("不可重复归一化"))

        builder._apply_tool_approval_settings([tool], approval_items)

        approval = tool.metadata["approval"]
        assert approval["target"]["display_name"] == "get_task_list"
        assert approval["target"]["name"] == "msdk-console-prod-mcp2__get_task_list"
        assert approval["target"]["code"] == "msdk-console-prod-mcp2__get_task_list"
        assert approval["tool_name"] == "msdk-console-prod-mcp2__get_task_list"

    def test_unchanged_tool_keeps_approval_metadata(self):
        settings = {
            "strategies": [{"strategy_id": 9, "approval_name": "脚本执行", "approvers": ["admin"]}],
            "bindings": [
                {
                    "resource_type": "tool",
                    "approval_enabled": True,
                    "approval_strategy_id": 9,
                    "tool_id": 3,
                    "tool_code": "python_code",
                }
            ],
        }
        builder = _builder(settings)
        builder.ctx.agent_config.resources = [{"type": "tool", "id": 3, "code": "python_code"}]
        tool = _make_tool("python_code", {"tool_code": "python_code", "tool_id": 3})

        builder._apply_tool_approval_settings([tool], builder._normalize_tool_approval_bindings())

        approval = tool.metadata["approval"]
        assert approval["target"]["display_name"] == "python_code"
        assert approval["target"]["name"] == "python_code"
        assert approval["target"]["code"] == "python_code"


class TestBuildToolsApprovalMatcher:
    def test_passes_matcher_derived_from_approval_bindings(self):
        builder = _builder(_approval_settings())
        builder.ctx.resource_manager.construct_mcp = MagicMock(return_value=MagicMock(tools=[], fetch_failures=[]))

        builder.build_tools()

        matcher = builder.ctx.resource_manager.construct_mcp.call_args.kwargs["approval_matcher"]
        assert callable(matcher)
        assert matcher("msdk-console-prod-mcp2", "get_task_list") is True
        assert matcher("msdk-console-prod-mcp2", "other_tool") is False

    def test_no_approval_config_passes_none(self):
        builder = _builder({})
        builder.ctx.resource_manager.construct_mcp = MagicMock(return_value=MagicMock(tools=[], fetch_failures=[]))

        builder.build_tools()

        assert builder.ctx.resource_manager.construct_mcp.call_args.kwargs["approval_matcher"] is None


@patch("aidev_agent.packages.resource_manager.base.MultiServerMCPClient")
def test_build_tools_same_service_approval_group_not_merged(mock_mcp_client_class):
    """同服务重名组命中审批配置：不合并、名字唯一，改名工具仍带审批 metadata。"""

    def _make_mcp_tool(name: str) -> MagicMock:
        tool = MagicMock(spec=StructuredTool)
        tool.name = name
        tool.description = f"{name} desc"
        tool.coroutine = AsyncMock()
        tool.metadata = {}
        tool.args = {"query": {"type": "string"}}
        return tool

    async def _get_tools(*, server_name):
        return [_make_mcp_tool("get_task_list")]

    client = MagicMock()
    client.get_tools = AsyncMock(side_effect=_get_tools)
    mock_mcp_client_class.return_value = client

    rm = AgentResourceManager(app_code="dummy_app", app_secret="dummy_secret")
    builder = _builder(_approval_settings(), resource_manager=rm)

    tools = builder.build_tools()

    names = [tool.name for tool in tools]
    assert names == ["get_task_list", "msdk-console-prod-mcp2__get_task_list"]
    assert len(names) == len(set(names))
    renamed = tools[1]
    assert renamed.metadata["original_name"] == "get_task_list"
    assert renamed.metadata["approval"]["target"]["display_name"] == "get_task_list"
    assert renamed.metadata["approval"]["target"]["name"] == "msdk-console-prod-mcp2__get_task_list"
