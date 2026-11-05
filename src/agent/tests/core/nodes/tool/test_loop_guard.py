# -*- coding: utf-8 -*-
"""工具循环调用拒绝：判定、拒绝、终止与配置。

用 fake 工具 + ``StateGraph(tools → END)`` 直接驱动工具节点，不接真实模型与 MCP。
"""

import os
import subprocess
import sys
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import Annotated, TypedDict

from aidev_agent.core.nodes.tool import ToolNodeSettings, build_tool_node
from aidev_agent.core.nodes.tool import loop_guard as loop_guard_module
from aidev_agent.core.nodes.tool.loop_guard import (
    TERMINATION_MESSAGE,
    TRUNCATED_SUFFIX,
    ToolLoopTerminationError,
    count_contiguous,
    fingerprint,
)


class _State(TypedDict):
    messages: Annotated[list, add_messages]


def _tools(counter: list) -> list:
    """构造一对 fake 工具：echo 记录真实执行次数，other 用于穿插。"""

    @tool
    def echo(text: str) -> str:
        """回显文本。"""
        counter.append(text)
        return f"echo:{text}#{len(counter)}"

    @tool
    def other(text: str) -> str:
        """另一个工具。"""
        return f"other:{text}"

    return [echo, other]


def _call(index: int, name: str = "echo", args: dict | None = None) -> dict:
    return {
        "name": name,
        "args": args if args is not None else {"text": "x"},
        "id": f"call-{index}",
        "type": "tool_call",
    }


def _round(index: int, *, name: str = "echo", args: dict | None = None, result: str = "echo:x") -> list:
    return [
        AIMessage(content="", tool_calls=[_call(index, name, args)]),
        ToolMessage(content=result, tool_call_id=f"call-{index}", name=name),
    ]


def _state(rounds: int, *, name: str = "echo", args: dict | None = None, result: str = "echo:x") -> dict:
    """构造 rounds 轮已完成历史 + 当前待执行的第 rounds 次调用。"""
    messages: list = [HumanMessage(content="重复调用测试")]
    for index in range(rounds):
        messages.extend(_round(index, name=name, args=args, result=result))
    messages.append(AIMessage(content="", tool_calls=[_call(rounds, name, args)]))
    return {"messages": messages}


def _graph(tools: list, *, settings: ToolNodeSettings | None = None, checkpointer: Any = None):
    node = build_tool_node(tools=tools, node_options=settings or ToolNodeSettings())
    graph = StateGraph(_State)
    graph.add_node("tools", node)
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    return graph.compile(checkpointer=checkpointer)


def _last_tool_message(result: dict) -> ToolMessage:
    return [msg for msg in result["messages"] if isinstance(msg, ToolMessage)][-1]


SUMMARY_HEADER = "最后一次真实返回内容摘要：\n"


def _summary_of(content: str) -> str:
    """取出拒绝反馈里「最后一次真实返回内容摘要」段。"""
    _, _, rest = str(content).partition(SUMMARY_HEADER)
    return rest.partition("请改用其他工具")[0].strip()


class TestFingerprint:
    @pytest.mark.parametrize(
        ("left", "right"),
        [({"b": 1, "a": 2}, {"a": 2, "b": 1}), ({"a": "x  y"}, {"a": "x  y"})],
    )
    def test_key_order_and_whitespace_insensitive(self, left, right):
        assert fingerprint(left) == fingerprint(right)

    @pytest.mark.parametrize(("left", "right"), [({"a": None}, {}), ({"a": 1}, {"a": "1"})])
    def test_distinguishes_null_missing_and_type(self, left, right):
        assert fingerprint(left) != fingerprint(right)

    def test_unserializable_returns_none(self):
        assert fingerprint({"a": {1, 2}}) is None


class TestCountContiguous:
    def test_empty_history(self):
        assert count_contiguous([], _call(0)) == 0

    @pytest.mark.parametrize("rounds", [1, 2, 3])
    def test_counts_previous_rounds(self, rounds):
        assert count_contiguous(_state(rounds)["messages"], _call(rounds)) == rounds

    def test_interleaved_tool_resets(self):
        messages = [HumanMessage(content="go")]
        messages.extend(_round(0))
        messages.extend(_round(1, name="other", result="other:x"))
        messages.append(AIMessage(content="", tool_calls=[_call(2)]))
        assert count_contiguous(messages, _call(2)) == 0

    def test_changed_args_resets(self):
        messages = [HumanMessage(content="go")]
        messages.extend(_round(0))
        messages.append(AIMessage(content="", tool_calls=[_call(1, args={"text": "y"})]))
        assert count_contiguous(messages, _call(1, args={"text": "y"})) == 0

    def test_same_batch_folds_to_one(self):
        messages = [
            HumanMessage(content="go"),
            AIMessage(content="", tool_calls=[_call(0), _call(1)]),
            ToolMessage(content="echo:x", tool_call_id="call-0", name="echo"),
            AIMessage(content="", tool_calls=[_call(2)]),
        ]
        assert count_contiguous(messages, _call(2)) == 1

    def test_window_starts_after_last_user_message(self):
        messages = [HumanMessage(content="old")]
        messages.extend(_round(0))
        messages.extend(_round(1))
        messages.append(HumanMessage(content="new"))
        messages.append(AIMessage(content="", tool_calls=[_call(5)]))
        assert count_contiguous(messages, _call(5)) == 0


class TestGuardBehaviour:
    def test_allow_within_threshold(self):
        counter: list = []
        result = _graph(_tools(counter)).invoke(_state(2))
        assert _last_tool_message(result).status == "success"
        assert counter == ["x"]

    def test_reject_at_threshold(self):
        counter: list = []
        message = _last_tool_message(_graph(_tools(counter)).invoke(_state(3)))
        assert message.status == "error"
        assert counter == []
        assert message.tool_call_id == "call-3"

    def test_rejection_feedback_fields(self):
        counter: list = []
        message = _last_tool_message(_graph(_tools(counter)).invoke(_state(3, result="上一轮返回内容")))
        content = message.content
        assert "echo" in content
        assert "4" in content
        assert "拒绝" in content
        assert "上一轮返回内容" in content
        assert "其他工具" in content and "调整" in content

    def test_rejection_feedback_truncates_long_result(self):
        counter: list = []
        settings = ToolNodeSettings(result_limit_thrd=20)
        message = _last_tool_message(_graph(_tools(counter), settings=settings).invoke(_state(3, result="y" * 100)))
        assert TRUNCATED_SUFFIX in message.content
        assert "y" * 100 not in message.content

    def test_feedback_round_still_rejects(self):
        counter: list = []
        message = _last_tool_message(_graph(_tools(counter)).invoke(_state(4)))
        assert message.status == "error"
        assert counter == []

    def test_two_rejections_track_last_real_result(self):
        """逐轮驱动 tools 节点：第 4、5 次拒绝的摘要都取自最近一次真实执行结果。"""
        counter: list = []
        graph = _graph(_tools(counter))
        messages: list = [HumanMessage(content="逐轮驱动")]
        results = []
        for index in range(5):
            messages = graph.invoke({"messages": [*messages, AIMessage(content="", tool_calls=[_call(index)])]})[
                "messages"
            ]
            results.append(_last_tool_message({"messages": messages}))

        first, second = results[3], results[4]
        assert (first.status, second.status) == ("error", "error")
        assert "已连续调用次数：4" in first.content
        assert "已连续调用次数：5" in second.content
        assert _summary_of(first.content) == "echo:x#3"
        assert _summary_of(second.content) == "echo:x#3"
        assert "拒绝" not in _summary_of(second.content)
        assert counter == ["x", "x", "x"]

    def test_terminate_after_feedback_rounds(self):
        counter: list = []
        with pytest.raises(ToolLoopTerminationError) as excinfo:
            _graph(_tools(counter)).invoke(_state(5))
        assert TERMINATION_MESSAGE in str(excinfo.value)
        assert "echo" in str(excinfo.value)
        assert counter == []

    def test_termination_keeps_previous_messages(self):
        counter: list = []
        graph = _graph(_tools(counter), checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "loop-termination"}}
        with pytest.raises(ToolLoopTerminationError):
            graph.invoke(_state(5), config=config)
        kept = [msg for msg in graph.get_state(config).values["messages"] if isinstance(msg, ToolMessage)]
        assert len(kept) == 5

    def test_interleaved_tool_is_allowed(self):
        counter: list = []
        messages = [HumanMessage(content="go")]
        messages.extend(_round(0))
        messages.extend(_round(1, name="other", result="other:x"))
        messages.append(AIMessage(content="", tool_calls=[_call(2)]))
        result = _graph(_tools(counter)).invoke({"messages": messages})
        assert _last_tool_message(result).status == "success"
        assert counter == ["x"]

    def test_decision_error_allows_call(self, monkeypatch):
        counter: list = []

        def _boom(*args, **kwargs):
            raise RuntimeError("判定失败")

        monkeypatch.setattr(loop_guard_module, "_scan", _boom)
        result = _graph(_tools(counter)).invoke(_state(3))
        assert _last_tool_message(result).status == "success"
        assert counter == ["x"]


class TestPolicyOverride:
    def test_custom_repeat_limit(self, monkeypatch):
        monkeypatch.setattr("aidev_agent.core.nodes.tool.node.BKAI_TOOL_LOOP_REPEAT_LIMIT", 5)
        counter: list = []
        assert _last_tool_message(_graph(_tools(counter)).invoke(_state(4))).status == "success"
        assert _last_tool_message(_graph(_tools(counter)).invoke(_state(5))).status == "error"

    def test_zero_feedback_rounds_terminates_on_first_repeat(self, monkeypatch):
        monkeypatch.setattr("aidev_agent.core.nodes.tool.node.BKAI_TOOL_LOOP_FEEDBACK_ROUNDS", 0)
        with pytest.raises(ToolLoopTerminationError):
            _graph(_tools([])).invoke(_state(3))


class TestConfig:
    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("BKAI_TOOL_LOOP_REPEAT_LIMIT", "0"),
            ("BKAI_TOOL_LOOP_REPEAT_LIMIT", "abc"),
            ("BKAI_TOOL_LOOP_FEEDBACK_ROUNDS", "-1"),
        ],
    )
    def test_invalid_value_fails_at_import(self, key, value):
        completed = subprocess.run(
            [sys.executable, "-c", "import aidev_agent.config"],
            capture_output=True,
            text=True,
            env={**os.environ, key: value},
            check=False,
        )
        assert completed.returncode != 0
        assert key in completed.stderr
