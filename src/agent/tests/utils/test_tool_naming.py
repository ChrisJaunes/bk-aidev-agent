# -*- coding: utf-8 -*-
"""utils/tool_naming.py 纯函数单元测试。"""

import re
from types import SimpleNamespace

import pytest

from aidev_agent.utils.tool_naming import (
    MAX_TOOL_NAME_LEN,
    compose_tool_name,
    dedupe_mcp_tools,
    is_same_service,
    sanitize,
    split_service,
)

NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _tool(name: str, mcp_name: str, args: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, metadata={"mcp_name": mcp_name}, args=args or {"q": {"type": "string"}})


class TestSplitService:
    @pytest.mark.parametrize(
        "server_name, expected",
        [
            ("msdk-console-prod-mcp", "msdk-console"),
            ("msdk-console-prod-mcp2", "msdk-console"),
            ("foo-mcp", None),
            ("a-prod-x-prod-y", None),
            ("-prod-mcp", None),
            ("svc-prod-", None),
            ("", None),
        ],
    )
    def test_split_service(self, server_name, expected):
        assert split_service(server_name) == expected


class TestIsSameService:
    def test_prefix_pair_is_same_service(self):
        assert is_same_service("msdk-console-prod-mcp", "msdk-console-prod-mcp2") is True

    @pytest.mark.parametrize(
        "server_a, server_b",
        [
            ("foo-mcp", "foo-mcp-admin"),
            ("a-prod-x-prod-y", "a-prod-x-prod-z"),
            ("a", "a"),
            ("foo-mcp", "foo-mcp2"),
            ("", "msdk-console-prod-mcp"),
        ],
    )
    def test_not_same_service(self, server_a, server_b):
        assert is_same_service(server_a, server_b) is False


class TestSanitize:
    def test_replace_illegal_chars(self):
        assert sanitize("msdk.console/prod mcp") == "msdk_console_prod_mcp"

    def test_keep_legal_chars(self):
        assert sanitize("a-b_c9") == "a-b_c9"


class TestComposeToolName:
    def test_short_name_uses_sep(self):
        assert compose_tool_name("msdk-console-prod-mcp2", "get_task_list") == ("msdk-console-prod-mcp2__get_task_list")

    def test_illegal_chars_sanitized(self):
        assert compose_tool_name("mcp.v1", "get task") == "mcp_v1__get_task"

    def test_overlong_name_truncated_with_hash(self):
        mcp_name = "a" * 50
        tool_name = "b" * 30
        result = compose_tool_name(mcp_name, tool_name)
        assert len(result) == MAX_TOOL_NAME_LEN
        assert NAME_RE.match(result)
        # 前缀被截断到 23 字符（64 - 9 后缀 - 2 分隔符 - 30 工具名）
        assert result.startswith("a" * 23 + "__" + "b" * 30)
        assert re.match(r"^a{23}__b{30}_[0-9a-f]{8}$", result)

    def test_overlong_tool_name_drops_prefix(self):
        result = compose_tool_name("mcp", "t" * 80)
        assert len(result) == MAX_TOOL_NAME_LEN
        assert NAME_RE.match(result)
        assert "__" not in result

    def test_deterministic(self):
        assert compose_tool_name("a" * 50, "b" * 30) == compose_tool_name("a" * 50, "b" * 30)


class TestDedupeMcpTools:
    def test_no_duplicate_returns_same_objects_and_no_change(self):
        tools = [_tool("a", "srv-prod-1"), _tool("b", "srv-prod-1")]
        result = dedupe_mcp_tools(tools)
        assert result.tools == tools
        assert result.changes == ()

    def test_same_service_keeps_first_without_rename(self):
        tools = [
            _tool("get_task_list", "msdk-console-prod-mcp"),
            _tool("get_task_list", "msdk-console-prod-mcp2"),
            _tool("other", "msdk-console-prod-mcp2"),
        ]
        result = dedupe_mcp_tools(tools)
        assert [t.name for t in result.tools] == ["get_task_list", "other"]
        assert result.tools[0].metadata["mcp_name"] == "msdk-console-prod-mcp"
        (change,) = result.changes
        assert change.kind == "merge"
        assert change.original == "get_task_list"
        assert change.server == "msdk-console-prod-mcp"
        assert change.merged_servers == ("msdk-console-prod-mcp2",)

    def test_cross_service_keeps_first_and_prefixes_others(self):
        tools = [
            _tool("get_task_list", "foo-mcp"),
            _tool("get_task_list", "bar-mcp"),
            _tool("get_task_list", "baz-mcp"),
        ]
        result = dedupe_mcp_tools(tools)
        assert [t.name for t in result.tools] == [
            "get_task_list",
            "bar-mcp__get_task_list",
            "baz-mcp__get_task_list",
        ]
        assert [c.kind for c in result.changes] == ["rename", "rename"]
        assert result.changes[0].server == "bar-mcp"

    def test_output_order_follows_input(self):
        tools = [
            _tool("dup", "foo-mcp"),
            _tool("solo", "other-mcp"),
            _tool("dup", "bar-mcp"),
        ]
        result = dedupe_mcp_tools(tools)
        assert [t.name for t in result.tools] == ["dup", "solo", "bar-mcp__dup"]

    def test_approval_matcher_exempts_whole_group(self):
        tools = [
            _tool("get_task_list", "msdk-console-prod-mcp"),
            _tool("get_task_list", "msdk-console-prod-mcp2"),
        ]

        result = dedupe_mcp_tools(tools, approval_matcher=lambda mcp, name: mcp.endswith("-mcp2"))

        assert [t.name for t in result.tools] == ["get_task_list", "msdk-console-prod-mcp2__get_task_list"]
        (change,) = result.changes
        assert change.kind == "rename"
        assert change.skip_merge_reason == "approval"

    def test_approval_matcher_false_keeps_merge(self):
        tools = [
            _tool("get_task_list", "msdk-console-prod-mcp"),
            _tool("get_task_list", "msdk-console-prod-mcp2"),
        ]
        result = dedupe_mcp_tools(tools, approval_matcher=lambda mcp, name: False)
        assert len(result.tools) == 1
        assert result.changes[0].kind == "merge"

    def test_rename_avoids_occupied_name(self):
        tools = [
            _tool("dup", "foo-mcp"),
            _tool("dup", "bar-mcp"),
            _tool("bar-mcp__dup", "baz-mcp"),
        ]
        result = dedupe_mcp_tools(tools)
        names = [t.name for t in result.tools]
        assert len(names) == len(set(names))
        assert names[0] == "dup"
        assert names[2] == "bar-mcp__dup"

    def test_schema_mismatch_flagged_on_merge(self):
        tools = [
            _tool("dup", "msdk-console-prod-mcp", args={"a": {"type": "string"}}),
            _tool("dup", "msdk-console-prod-mcp2", args={"b": {"type": "integer"}}),
        ]
        result = dedupe_mcp_tools(tools)
        assert result.changes[0].schema_mismatch is True
