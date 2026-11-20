# -*- coding: utf-8 -*-
"""``AcpAdapter``：协议翻译层（ACP 请求 → RuntimeService 生命周期 → ACP 响应/通知）。

本文件的每条用例都对应一个**可点击的行为**，其中 B1/B2/B3 是原 worker 的真实缺陷
（由下列回归用例覆盖）。断言的是**当前契约**，不是旧行为：

* 失败轮必须让对端看到 error（B3），不能是 ``end_turn``；
* 取消只命中目标 session（B1）；
* 并发会话的通知与工具参数不串（B2）。

替身只替**执行后端**（``RuntimeService`` 的 strategy），不替协议层：ACP 的
``AcpAdapter`` 走真实实现，这样测到的才是线上路径。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from acp.exceptions import RequestError

from aidev_agent_cli.adapters.acp import (
    AcpAdapter,
    AguiEventInterpreter,
    _AcpSink,
    _text_of_content_blocks,
    _tool_kind,
)
from aidev_agent_cli.runtime.contracts import RunOutcome, SessionOptions
from aidev_agent_cli.runtime.service import RuntimeService


class _FakeConn:
    """记录 ``session_update`` 通知的假连接（SDK 的 ``AgentSideConnection`` 只用到这一面）。"""

    def __init__(self) -> None:
        self.updates: list[tuple[str, Any]] = []

    async def session_update(self, session_id: str, update: Any) -> None:
        self.updates.append((session_id, update))

    def texts(self, session_id: str | None = None) -> list[str]:
        out = []
        for sid, update in self.updates:
            if session_id is not None and sid != session_id:
                continue
            content = getattr(update, "content", None)
            text = getattr(content, "text", None)
            if isinstance(text, str):
                out.append(text)
        return out

    def tool_ids(self, session_id: str | None = None) -> list[str]:
        out = []
        for sid, update in self.updates:
            if session_id is not None and sid != session_id:
                continue
            tool_call_id = getattr(update, "tool_call_id", None)
            if tool_call_id is not None:
                out.append(tool_call_id)
        return out


class _StubRunner:
    """替身执行策略：按脚本产出事件并落定终态。

    ``plans`` 是 ``session_code -> {"events": [(payload, delay), ...], "status": ...,``。
    ``delay`` 让 cancel 有窗口；``status`` 决定 ``RunOutcome``。
    """

    backend = "platform"

    def __init__(self, plans: dict[str, dict[str, Any]]) -> None:
        self._plans = plans
        self.opened: list[SessionOptions] = []
        self.closed: list[Any] = []

    def open(self, options: SessionOptions, process: Any = None) -> Any:
        self.opened.append(options)

        return _FakeEnvironment(options.session_code)

    def close(self, backend: Any) -> None:
        self.closed.append(backend)

    async def execute(self, *, handle: Any, snapshot: Any, sink: Any, session: Any, **kwargs) -> RunOutcome:
        plan = self._plans.get(snapshot.session.session_code, {})
        for payload, delay in plan.get("events", []):
            if delay:
                await asyncio.sleep(delay)
            if handle.token.is_cancelled():
                return RunOutcome(status="cancelled", run_id=handle.run_id, session_code=snapshot.session.session_code)
            await sink.send(payload)

        return RunOutcome(
            status=plan.get("status", "finished"),
            run_id=handle.run_id,
            session_code=snapshot.session.session_code,
            error_code=plan.get("error_code", ""),
            error_message=plan.get("error_message", ""),
        )



class _FakeEnvironment:
    """最小会话后端：``set_access_token`` 是 RuntimeService.create 会探测的唯一方法。"""

    def __init__(self, session_code: str) -> None:
        self.session_code = session_code
        self.token_calls: list[tuple[str, bool]] = []

    def set_access_token(self, token: str, *, persist: bool = True) -> None:
        self.token_calls.append((token, persist))


async def _adapter(
    plans: dict[str, dict[str, Any]] | None = None,
) -> tuple[AcpAdapter, _FakeConn, _StubRunner, RuntimeService]:
    """建 adapter，并**预置**好 ``plans`` 里点名的会话。

    ``new_session`` 的 ``sessionId`` 是随机生成的（它必须与平台 session_code 一致），
    但用例要按名字脚本化 —— 因此这里先按 ``plans`` 的键登记会话，等价于客户端已经
    ``session/new`` 过。需要验证 ``new_session`` 本身的用例自己再调它。
    """
    strategy = _StubRunner(plans or {})
    service = RuntimeService()
    service._scheduler._runner = strategy
    original_create = service.new_session

    async def create(options=None, **kwargs):
        info = await original_create(options, **kwargs)
        strategy.opened.append(options)
        return info

    service.new_session = create
    for session_code in plans or {}:
        await service.new_session(SessionOptions( session_code=session_code))
    conn = _FakeConn()
    adapter = AcpAdapter(service=service)
    adapter.on_connect(conn)

    return adapter, conn, strategy, service


def _text_blocks(*texts: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text} for text in texts]


# ============================================================================
# initialize / new_session
# ============================================================================


async def test_initialize_declares_protocol_one_and_no_unimplemented_capability() -> None:
    """能力必须**诚实**：只宣告已实现的那一面，未实现的一律 false。"""
    adapter, _conn, _strategy, _service = await _adapter()

    response = await adapter.initialize(protocol_version=1)

    assert response.protocol_version == 1
    capabilities = response.agent_capabilities
    assert capabilities.load_session is False
    assert capabilities.prompt_capabilities.image is False
    assert capabilities.prompt_capabilities.audio is False
    assert capabilities.prompt_capabilities.embedded_context is False


async def test_new_session_maps_acp_session_id_to_the_platform_session_code() -> None:
    """``sessionId`` 就是平台 ``session_code``——不另立第二个 id 需要同步。"""
    adapter, _conn, strategy, service = await _adapter()

    response = await adapter.new_session(cwd="")

    assert response.session_id
    assert (await service.session_info(response.session_id)) is not None
    assert [options.session_code for options in strategy.opened] == [response.session_id]


async def test_two_sessions_are_isolated_by_session_code_not_cwd(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """工作区根固定为进程 cwd：两个会话由各自的 session_code 区分，cwd 只做诊断记录。"""
    import os

    monkeypatch.delenv("AGENT_TUI_CWD", raising=False)
    first = tmp_path / "one"
    second = tmp_path / "two"
    first.mkdir()
    second.mkdir()

    adapter, _conn, strategy, _service = await _adapter()
    one = await adapter.new_session(cwd=str(first))
    two = await adapter.new_session(cwd=str(second))

    assert one.session_id != two.session_id
    assert [options.session_code for options in strategy.opened] == [one.session_id, two.session_id]
    # cwd 不再写入 SessionOptions，也不写进程级环境变量。
    assert not any(hasattr(options, "workspace_root") for options in strategy.opened)
    assert "AGENT_TUI_CWD" not in os.environ


async def test_a_missing_cwd_is_still_accepted() -> None:
    """不存在的 cwd 只是被记录：不再校验目录、也不影响工作区（固定为进程 cwd）。"""
    adapter, _conn, strategy, _service = await _adapter()

    response = await adapter.new_session(cwd="/definitely/not/a/directory")

    assert [options.session_code for options in strategy.opened] == [response.session_id]
    assert not hasattr(strategy.opened[0], "workspace_root")


# ============================================================================
# prompt —— 终态映射（B3）
# ============================================================================


async def test_finished_turn_maps_to_end_turn() -> None:
    adapter, _conn, _strategy, _service = await _adapter(
        {"s1": {"events": [({"type": "TEXT_MESSAGE_CONTENT", "delta": "hi"}, 0)], "status": "finished"}}
    )
    response = await adapter.prompt("s1", _text_blocks("hello"))

    assert response.stop_reason == "end_turn"


async def test_a_failed_turn_raises_instead_of_claiming_end_turn() -> None:
    """**B3 回归**：失败轮抛 ``AcpError`` → SDK 回 JSON-RPC error，绝不谎称成功。

    原实现里 ``_AcpFrameWriter`` 只转发 ``kind == "event"`` 的帧，错误帧被丢弃，于是
    runner 的异常变成 ``end_turn``。这条用例把「失败必须能被说成失败」钉住。
    """
    adapter, _conn, _strategy, _service = await _adapter(
        {"s1": {"events": [], "status": "failed", "error_message": "模型不可用"}}
    )
    with pytest.raises(RequestError) as caught:
        await adapter.prompt("s1", _text_blocks("hello"))

    assert "模型不可用" in str(caught.value)


async def test_a_rejected_turn_also_raises() -> None:
    """busy / 未知会话的 rejected 终态同样是对端的错误，不是一次成功的空轮。"""
    adapter, _conn, _strategy, _service = await _adapter(
        {"s1": {"events": [], "status": "rejected", "error_message": "会话已有进行中的一轮"}}
    )
    await adapter.new_session(cwd="")

    with pytest.raises(RequestError):
        await adapter.prompt("s1", _text_blocks("hello"))


async def test_the_loop_keeps_serving_after_a_failed_prompt() -> None:
    """失败只影响这一轮：同一 adapter 的下一个 prompt 仍然工作。"""
    adapter, _conn, _strategy, _service = await _adapter({"s1": {"events": [], "status": "finished"}})
    await adapter.new_session(cwd="")

    with pytest.raises(RequestError):
        # 未建立的会话 → 明确失败，而不是一个假的 end_turn。
        await adapter.prompt("no-such-session", _text_blocks("hello"))

    response = await adapter.prompt("s1", _text_blocks("again"))
    assert response.stop_reason == "end_turn"


async def test_an_interrupted_turn_reports_end_turn_and_emits_the_text() -> None:
    """中断降级保持诚实：文本已作为 ``agent_message_chunk`` 发出，**不**发 permission 请求。"""
    interrupt_event = {
        "type": "RUN_FINISHED",
        "outcome": {"type": "interrupt", "interrupts": [{"reason": "aidev:tool_approval"}]},
    }
    adapter, conn, _strategy, _service = await _adapter({"s1": {"events": [(interrupt_event, 0)], "status": "interrupted"}})
    response = await adapter.prompt("s1", _text_blocks("do it"))

    assert response.stop_reason == "end_turn"
    # 中断被压成一段人可读文本（而不是一个 permission 请求）。
    assert any("审批请求" in text for text in conn.texts("s1"))


async def test_an_unknown_session_is_an_error_not_an_empty_turn() -> None:
    adapter, _conn, _strategy, _service = await _adapter()

    with pytest.raises(RequestError):
        await adapter.prompt("never-created", _text_blocks("hello"))


# ============================================================================
# prompt —— 通知内容（B2）
# ============================================================================


async def test_text_deltas_arrive_as_agent_message_chunks_on_the_right_session() -> None:
    adapter, conn, _strategy, _service = await _adapter(
        {
            "s1": {
                "events": [
                    ({"type": "TEXT_MESSAGE_CONTENT", "delta": "你"}, 0),
                    ({"type": "TEXT_MESSAGE_CONTENT", "delta": "好"}, 0),
                ]
            }
        }
    )
    await adapter.prompt("s1", _text_blocks("hi"))

    assert conn.texts("s1") == ["你", "好"]
    assert conn.texts("other") == []


async def test_two_concurrent_sessions_do_not_cross_tool_call_ids() -> None:
    """**B2 回归**：并发时工具参数/通知不串（原实现共享一个解释器 + 一个 writer）。

    两个会话各起一轮，A 的工具调用 id 必须只出现在 A 的通知里，反之亦然。
    """
    adapter, conn, _strategy, _service = await _adapter(
        {
            "s1": {
                "events": [
                    ({"type": "TOOL_CALL_START", "toolCallId": "a-tool", "toolCallName": "read_file"}, 0.01),
                    ({"type": "TOOL_CALL_ARGS", "toolCallId": "a-tool", "delta": '{"p":1}'}, 0),
                    ({"type": "TOOL_CALL_END", "toolCallId": "a-tool"}, 0),
                    ({"type": "TOOL_CALL_RESULT", "toolCallId": "a-tool", "content": "A"}, 0),
                ]
            },
            "s2": {
                "events": [
                    ({"type": "TOOL_CALL_START", "toolCallId": "b-tool", "toolCallName": "grep"}, 0.01),
                    ({"type": "TOOL_CALL_ARGS", "toolCallId": "b-tool", "delta": '{"q":"x"}'}, 0),
                    ({"type": "TOOL_CALL_END", "toolCallId": "b-tool"}, 0),
                    ({"type": "TOOL_CALL_RESULT", "toolCallId": "b-tool", "content": "B"}, 0),
                ]
            },
        }
    )
    await asyncio.gather(
        adapter.prompt("s1", _text_blocks("go")),
        adapter.prompt("s2", _text_blocks("go")),
    )

    assert set(conn.tool_ids("s1")) == {"a-tool"}
    assert set(conn.tool_ids("s2")) == {"b-tool"}


async def test_tool_arguments_are_rendered_as_json_object_input() -> None:
    adapter, conn, _strategy, _service = await _adapter(
        {
            "s1": {
                "events": [
                    ({"type": "TOOL_CALL_START", "toolCallId": "t1", "toolCallName": "bash"}, 0),
                    ({"type": "TOOL_CALL_ARGS", "toolCallId": "t1", "delta": '{"cmd":"ls"}'}, 0),
                    ({"type": "TOOL_CALL_END", "toolCallId": "t1"}, 0),
                ]
            }
        }
    )
    await adapter.prompt("s1", _text_blocks("go"))

    inputs = [getattr(update, "raw_input", None) for _sid, update in conn.updates]
    assert {"cmd": "ls"} in inputs


# ============================================================================
# cancel（B1）
# ============================================================================


async def test_cancel_only_hits_the_target_session() -> None:
    """**B1 回归**：取消 A 不得杀掉 B（原 ``_live_runs_for`` 忽略 ``session_id``）。"""
    adapter, _conn, _strategy, _service = await _adapter(
        {
            "s1": {"events": [({"type": "TEXT_MESSAGE_CONTENT", "delta": "x"}, 0.4)]},
            "s2": {"events": [({"type": "TEXT_MESSAGE_CONTENT", "delta": "y"}, 0.4)]},
        }
    )
    first = asyncio.create_task(adapter.prompt("s1", _text_blocks("go")))
    second = asyncio.create_task(adapter.prompt("s2", _text_blocks("go")))
    await asyncio.sleep(0.05)

    await adapter.cancel("s1")

    outcomes = await asyncio.gather(first, second, return_exceptions=True)
    assert outcomes[0].stop_reason == "cancelled"
    assert outcomes[1].stop_reason == "end_turn"


async def test_cancel_on_an_unknown_session_is_a_no_op() -> None:
    """协议允许取消一个已不存在的会话：这是合法空操作，不是异常。"""
    adapter, _conn, _strategy, _service = await _adapter()

    assert await adapter.cancel("nope") is None


async def test_cancel_after_the_turn_settled_is_a_no_op() -> None:
    adapter, _conn, _strategy, _service = await _adapter({"s1": {"events": [], "status": "finished"}})
    await adapter.prompt("s1", _text_blocks("go"))

    # 不抛异常即可（服务端会记一条 no-op 日志）。
    assert await adapter.cancel("s1") is None


# ============================================================================
# 辅助函数（翻译与输入的窄面）
# ============================================================================


def test_tool_kind_is_a_display_heuristic_with_other_as_fallback() -> None:
    assert _tool_kind("BashTool") == "execute"
    assert _tool_kind("read_file") == "read"
    assert _tool_kind("grep_files") == "search"
    assert _tool_kind("something-unknown") == "other"


def test_only_text_blocks_become_input_text() -> None:
    """非文本块被忽略（不宣告 image/audio/embedded 能力），而不是被误当文本。"""
    blocks = [
        {"type": "text", "text": "前"},
        {"type": "image", "data": "...", "mimeType": "image/png"},
        {"type": "text", "text": "后"},
    ]
    assert _text_of_content_blocks(blocks) == "前后"
    assert _text_of_content_blocks(None) == ""
    assert _text_of_content_blocks([]) == ""


def test_the_sink_holds_its_own_interpreter_per_run() -> None:
    """B2 的结构性证据：两个 sink 不共享解释器（``__slots__`` 里各有一份）。"""
    one = _AcpSink(_FakeConn(), "s1", AguiEventInterpreter())
    two = _AcpSink(_FakeConn(), "s2", AguiEventInterpreter())

    assert one._interpreter is not two._interpreter


async def test_sink_without_a_connection_is_a_safe_no_op() -> None:
    """尚未 on_connect 时（防御路径）不得抛异常——降级路径本身不能打断运行。"""
    await _AcpSink(None, "s1", AguiEventInterpreter()).send({"type": "TEXT_MESSAGE_CONTENT", "delta": "x"})


# ============================================================================
# _aidev/* 扩展：acp 与 acp-dsh 同一组会话参数 / 结果元数据
# ============================================================================


def test_session_meta_maps_to_the_same_session_options_for_both_acp_protocols() -> None:
    from aidev_agent_cli.adapters.acp import session_options_from_meta

    options = session_options_from_meta(
        {"agent": {"agentCode": "a1"}, "llm": {"model": "m1", "baseUrl": "http://gw", "maxTokens": 9}, "username": "u"},
        session_code="s1",
        tool_factories=(),
    )

    assert (options.agent_code, options.model, options.base_url, options.max_tokens) == ("a1", "m1", "http://gw", 9)
    assert options.username == "u"
    assert not hasattr(options, "workspace_root")
    assert not hasattr(options, "hooks"), "hook transport/configuration belongs to a run, not SessionOptions"


def test_outcome_meta_reports_interrupts() -> None:
    from aidev_agent_cli.adapters.acp import outcome_meta

    meta = outcome_meta(RunOutcome(status="interrupted", turn_id="t1", interrupts=[{"interruptId": "i1"}]))

    assert meta == {"status": "interrupt", "turnId": "t1", "interrupts": [{"interruptId": "i1"}]}


@pytest.mark.parametrize("fails", [False, True])
async def test_eof_waits_for_a_silent_handler_response(monkeypatch: pytest.MonkeyPatch, fails: bool) -> None:
    import io
    from types import SimpleNamespace

    from acp import run_agent

    from aidev_agent_cli.adapters import acp

    output = io.StringIO()
    monkeypatch.setattr(
        acp.sys,
        "stdin",
        SimpleNamespace(
            buffer=io.BytesIO(b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}\n')
        ),
    )
    monkeypatch.setattr(acp, "EOF_QUIET_SECONDS", 0.01)
    monkeypatch.setattr(acp, "EOF_DRAIN_SECONDS", 1.0)
    adapter, _conn, _strategy, _service = await _adapter()
    initialize = adapter.initialize

    async def delayed_initialize(**kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        if fails:
            raise RequestError(-32603, "handler failed")
        return await initialize(**kwargs)

    monkeypatch.setattr(adapter, "initialize", delayed_initialize)
    transport = acp._StdioTransport(output)
    await asyncio.wait_for(run_agent(adapter, transport), timeout=2)

    import json

    response = json.loads(output.getvalue())
    assert response["id"] == 1
    assert ("error" if fails else "result") in response
    assert transport._pending_requests == set()


async def test_eof_drain_has_a_hard_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from types import SimpleNamespace

    from aidev_agent_cli.adapters import acp

    monkeypatch.setattr(
        acp.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"id":"pending","method":"session/prompt"}\n'))
    )
    output = io.StringIO()
    monkeypatch.setattr(acp, "EOF_DRAIN_SECONDS", 0.03)
    transport = acp._StdioTransport(output)
    assert await transport.receive() == {"id": "pending", "method": "session/prompt"}
    assert await asyncio.wait_for(transport.receive(), timeout=0.5) is None
    assert transport._pending_requests == {"pending"}
    await transport.close()


async def test_outgoing_hook_request_does_not_settle_an_incoming_request(monkeypatch: pytest.MonkeyPatch) -> None:
    import io
    from types import SimpleNamespace

    from aidev_agent_cli.adapters import acp

    output = io.StringIO()
    monkeypatch.setattr(acp.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"id":1,"method":"initialize"}\n')))
    transport = acp._StdioTransport(output)
    await transport.receive()
    await transport.send({"id": 1, "method": "_aidev/hook", "params": {}})
    assert transport._pending_requests == {1}
    await transport.send({"id": 1, "result": {}})
    assert transport._pending_requests == set()
    await transport.close()


def test_malformed_tool_start_does_not_log_payload(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    from aidev_agent_cli.adapters.acp import logger

    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="aidev_agent_cli.adapters.acp"):
            assert AguiEventInterpreter().interpret({"type": "TOOL_CALL_START", "secret": "private-payload"}) == []
    finally:
        logger.removeHandler(caplog.handler)
    assert "without usable toolCallId" in caplog.text
    assert "private-payload" not in caplog.text