# -*- coding: utf-8 -*-
"""Tests for the tool-result limit wrapper (aidev_agent.core.nodes.tool.result_limit_wrapper)."""

from __future__ import annotations

from types import SimpleNamespace

from aidev_agent.core.nodes.tool.result_limit_wrapper import (
    TOOL_RESULT_TOO_LONG_MESSAGE,
    build_result_limit_sync_wrapper,
)
from langchain_core.messages import ToolMessage


def _make_request(name: str) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name, "id": "id-1"}, tool=None, state={})


class TestResultLimitWrapper:
    def test_short_result_unchanged(self):
        wrapper = build_result_limit_sync_wrapper(100)
        msg = ToolMessage(content="short", tool_call_id="id-1", name="t")
        result = wrapper(_make_request("t"), lambda _req: msg)
        assert result.content == "short"

    def test_long_string_minimized_truncated(self):
        wrapper = build_result_limit_sync_wrapper(100)
        content = "HEAD-" + "M" * 1000 + "-TAIL"
        msg = ToolMessage(content=content, tool_call_id="id-1", name="t")
        result = wrapper(_make_request("t"), lambda _req: msg)
        # 保头保尾、中间丢弃，不再是整段拒绝
        assert result.content.startswith("HEAD-")
        assert result.content.endswith("-TAIL")
        assert result.content.count("M") < 1000
        assert "截断" in result.content

    def test_structured_content_falls_back_to_reject(self):
        wrapper = build_result_limit_sync_wrapper(10)
        msg = ToolMessage(content=[{"type": "text", "text": "x" * 100}], tool_call_id="id-1", name="t")
        result = wrapper(_make_request("t"), lambda _req: msg)
        assert result.content == TOOL_RESULT_TOO_LONG_MESSAGE
        assert result.status == "error"

    def test_explicit_keep_head_tail(self):
        wrapper = build_result_limit_sync_wrapper(100, keep_head=10, keep_tail=5)
        content = "A" * 20 + "B" * 1000 + "C" * 10
        msg = ToolMessage(content=content, tool_call_id="id-1", name="t")
        result = wrapper(_make_request("t"), lambda _req: msg)
        assert result.content.startswith("A" * 10)
        assert result.content.endswith("C" * 5)
