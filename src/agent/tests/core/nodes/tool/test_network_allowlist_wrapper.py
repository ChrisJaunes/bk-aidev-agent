# -*- coding: utf-8 -*-
"""Tests for the network/domain allowlist tool wrapper (aidev_agent.core.nodes.tool.security_wrapper)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aidev_agent.core.nodes.tool.security_wrapper import build_network_allowlist_sync_wrapper
from langchain_core.messages import ToolMessage


def _make_request(name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name, "id": "id-1", "args": args}, tool=None, state={})


def _execute_ok(_req) -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id="id-1", name="http_get")


def _run(url: str, allow: list[str], block: list[str]):
    """按显式策略参数执行一次包装器，返回 (结果, 原工具是否被调用次数)。"""
    wrapper = build_network_allowlist_sync_wrapper(enabled=True, allow_domains=allow, block_domains=block)
    executed = {"n": 0}

    def execute(_req):
        executed["n"] += 1
        return ToolMessage(content="ok", tool_call_id="id-1", name="http_get")

    return wrapper(_make_request("http_get", {"url": url}), execute), executed["n"]


class TestNetworkAllowlistWrapper:
    def test_blocks_non_allowlisted_domain(self):
        result, calls = _run("https://evil.com/x", ["good.com"], [])
        assert result.status == "error"
        assert "evil.com" in result.content
        assert calls == 0  # 未执行原工具

    def test_allows_allowlisted_domain(self):
        result, calls = _run("https://good.com/x", ["good.com"], [])
        assert result.content == "ok"
        assert calls == 1

    def test_blocks_private_ip_even_with_allow_star(self):
        result, _ = _run("http://10.0.0.1/admin", ["*"], [])
        assert result.status == "error"

    def test_fail_open_when_no_config(self):
        result, calls = _run("https://any.com/x", [], [])
        assert result.content == "ok"
        assert calls == 1

    def test_block_list_wins(self):
        result, _ = _run("https://bad.good.com/x", ["good.com"], ["bad.good.com"])
        assert result.status == "error"

    @pytest.mark.parametrize("enabled", [False])
    def test_disabled_passes_through(self, enabled):
        wrapper = build_network_allowlist_sync_wrapper(enabled=enabled, allow_domains=["good.com"], block_domains=[])
        result = wrapper(_make_request("http_get", {"url": "https://evil.com/x"}), _execute_ok)
        assert result.content == "ok"
