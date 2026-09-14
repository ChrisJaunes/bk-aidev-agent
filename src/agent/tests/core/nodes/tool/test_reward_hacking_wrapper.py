# -*- coding: utf-8 -*-
"""Tests for the reward hacking tool wrapper (aidev_agent.core.nodes.tool.security_wrapper)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from aidev_agent.core.nodes.tool.security_wrapper import build_reward_hacking_sync_wrapper
from aidev_agent.packages.security.reward_hacking import _ledger
from langchain_core.messages import ToolMessage


@pytest.fixture(autouse=True)
def _clear_ledger():
    _ledger._sessions.clear()
    yield
    _ledger._sessions.clear()


def _make_request(name: str, args: dict, thread_id: str = "t1") -> SimpleNamespace:
    runtime = SimpleNamespace(config={"configurable": {"thread_id": thread_id}})
    return SimpleNamespace(
        tool_call={"name": name, "id": "id-1", "args": args},
        tool=None,
        state={},
        runtime=runtime,
    )


def _execute_ok(_req) -> ToolMessage:
    return ToolMessage(content="ok", tool_call_id="id-1", name="exec")


class TestRewardHackingWrapper:
    def test_first_dangerous_intent_passes(self):
        wrapper = build_reward_hacking_sync_wrapper(enabled=True, allow_domains=None, block_domains=None)
        result = wrapper(_make_request("exec", {"cmd": "rm -rf /etc"}), _execute_ok)
        assert result.content == "ok"  # 首次放行，交由硬约束拦截

    def test_repeated_intent_escalates(self):
        wrapper = build_reward_hacking_sync_wrapper(enabled=True, allow_domains=None, block_domains=None)
        executed = {"n": 0}

        def execute(req):
            executed["n"] += 1
            return ToolMessage(content="ok", tool_call_id="id-1", name="exec")

        req = _make_request("exec", {"cmd": "rm -rf /etc"})
        wrapper(req, execute)
        result = wrapper(req, execute)
        assert "Reward Hacking" in result.content
        assert result.status == "error"
        assert executed["n"] == 1  # 第二次未再执行原工具

    def test_rephrased_intent_converges_and_escalates(self):
        wrapper = build_reward_hacking_sync_wrapper(enabled=True, allow_domains=None, block_domains=None)
        req1 = _make_request("exec", {"cmd": "rm -rf /etc"})
        req2 = _make_request("shell", {"cmd": "rm -fr /etc"})  # 换工具 + 换措辞，同一目标
        wrapper(req1, _execute_ok)
        result = wrapper(req2, _execute_ok)
        assert "Reward Hacking" in result.content

    def test_clean_intent_never_escalates(self):
        wrapper = build_reward_hacking_sync_wrapper(enabled=True, allow_domains=None, block_domains=None)
        req = _make_request("web_search", {"query": "今天天气"})
        assert wrapper(req, _execute_ok).content == "ok"
        assert wrapper(req, _execute_ok).content == "ok"

    def test_threads_isolated(self):
        wrapper = build_reward_hacking_sync_wrapper(enabled=True, allow_domains=None, block_domains=None)
        req_a = _make_request("exec", {"cmd": "rm -rf /etc"}, thread_id="ta")
        req_b = _make_request("exec", {"cmd": "rm -rf /etc"}, thread_id="tb")
        wrapper(req_a, _execute_ok)
        # 不同 thread 互不影响，tb 首次仍放行
        assert wrapper(req_b, _execute_ok).content == "ok"
