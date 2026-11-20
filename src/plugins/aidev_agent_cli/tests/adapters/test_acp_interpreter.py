# -*- coding: utf-8 -*-
"""喂入 AG-UI payload，断言直接输出的 ACP 消息及工具参数累积行为。"""

from __future__ import annotations

from acp.schema import AgentMessageChunk, AgentThoughtChunk, TextContentBlock, ToolCallProgress, ToolCallStart

from aidev_agent_cli.adapters.acp import AguiEventInterpreter


def _interp(*payloads: dict) -> list:
    interpreter = AguiEventInterpreter()
    out: list = []
    for payload in payloads:
        out.extend(interpreter.interpret(payload))
    return out


def test_text_and_thinking_deltas_stay_separate_channels() -> None:
    updates = _interp(
        {"type": "THINKING_TEXT_MESSAGE_CONTENT", "delta": "想"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "说"},
    )
    assert updates == [
        AgentThoughtChunk(content=TextContentBlock(text="想")),
        AgentMessageChunk(content=TextContentBlock(text="说")),
    ]


def test_empty_or_malformed_delta_emits_nothing() -> None:
    assert _interp({"type": "TEXT_MESSAGE_CONTENT", "delta": ""}) == []
    assert _interp({"type": "TEXT_MESSAGE_CONTENT", "delta": None}) == []
    assert _interp({"type": "THINKING_TEXT_MESSAGE_CONTENT"}) == []


def test_tool_args_accumulate_until_end_and_parse_to_object() -> None:
    updates = _interp(
        {"type": "TOOL_CALL_START", "toolCallId": "t1", "toolCallName": "get_weather"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t1", "delta": '{"city":"'},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t1", "delta": '北京"}'},
        {"type": "TOOL_CALL_END", "toolCallId": "t1"},
    )
    assert updates == [
        ToolCallStart(tool_call_id="t1", name="get_weather", title="get_weather", kind="other", status="in_progress"),
        ToolCallProgress(status="in_progress", tool_call_id="t1", raw_input={"city": "北京"}),
    ]


def test_non_json_args_degrade_to_none_not_a_string() -> None:
    updates = _interp(
        {"type": "TOOL_CALL_START", "toolCallId": "t2", "toolCallName": "bash"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t2", "delta": "echo hi"},
        {"type": "TOOL_CALL_END", "toolCallId": "t2"},
    )
    assert updates[-1] == ToolCallProgress(status="in_progress", tool_call_id="t2", raw_input=None)


def test_result_without_end_flushes_args_then_finishes() -> None:
    interpreter = AguiEventInterpreter()
    updates: list = []
    for payload in (
        {"type": "TOOL_CALL_START", "toolCallId": "t3", "toolCallName": "bash"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t3", "delta": '{"x":1}'},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "t3", "content": "hi"},
    ):
        updates.extend(interpreter.interpret(payload))
    assert updates == [
        ToolCallStart(tool_call_id="t3", name="bash", title="bash", kind="execute", status="in_progress"),
        ToolCallProgress(status="in_progress", tool_call_id="t3", raw_input={"x": 1}),
        ToolCallProgress(status="completed", tool_call_id="t3", raw_output="hi"),
    ]
    # Accumulation is cleaned on every exit path — no leak across turns.
    assert interpreter._tool_calls == {}


def test_tool_call_start_without_id_is_skipped() -> None:
    assert _interp({"type": "TOOL_CALL_START", "toolCallName": "x"}) == []


def test_run_finished_interrupt_downgrades_to_lines() -> None:
    updates = _interp(
        {
            "type": "RUN_FINISHED",
            "outcome": {
                "type": "interrupt",
                "interrupts": [
                    {"reason": "aidev:tool_approval", "metadata": {"title": "Deploy target"}},
                    {
                        "reason": "aidev:user_question",
                        "metadata": {"questions": [{"header": "环境"}]},
                    },
                ],
            },
        }
    )
    assert updates == [
        AgentMessageChunk(content=TextContentBlock(text="[审批请求]")),
        AgentMessageChunk(content=TextContentBlock(text="[需要你回答问题] 环境")),
    ]


def test_run_finished_without_interrupt_emits_nothing() -> None:
    assert _interp({"type": "RUN_FINISHED"}) == []
    assert _interp({"type": "RUN_FINISHED", "outcome": {"type": "success"}}) == []


def test_unknown_event_type_is_skipped_not_fatal() -> None:
    assert _interp({"type": "SOME_FUTURE_EVENT", "payload": {"x": 1}}) == []
