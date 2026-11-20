# -*- coding: utf-8 -*-
"""进程级 hook handler：``--hooks`` 解析、后绑定通道、超时 / 取消收敛，以及 AG-UI 的 hook 帧往返。"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from aidev_agent_cli.adapters.agui import AguiAdapter, AguiTransport
from aidev_agent_cli.runtime.handles import CancelToken
from aidev_agent_cli.utils.errors import HeadlessError
from aidev_agent_cli.hooks.handler import parse_hooks_option
from aidev_agent_cli.hooks.models import HookRequest


def _request(timeout_ms: int = 1000) -> HookRequest:
    return HookRequest("req-1#h1", {"session_code": "s1", "point": "tool_pre", "payload": {}}, timeout_ms)


def test_parse_hooks_option(tmp_path) -> None:
    assert parse_hooks_option(None) is None
    handler = parse_hooks_option('{"timeout_ms": 500, "points": ["tool_pre"]}')
    assert handler.config.timeout_ms == 500 and handler.config.points() == {"tool_pre"}
    path = tmp_path / "hooks.json"
    path.write_text('{"points": ["turn_start"]}', encoding="utf-8")
    assert parse_hooks_option(f"@{path}").config.points() == {"turn_start"}
    for raw in ("not json", '{"timeout_ms": 0}', '{"points": ["nope"]}', f"@{tmp_path / 'missing'}"):
        with pytest.raises(HeadlessError) as exc:
            parse_hooks_option(raw)
        assert exc.value.code == "hook.value_invalid"


async def test_unattached_handler_times_out() -> None:
    reply = await parse_hooks_option("{}")(_request(), CancelToken())
    assert reply.status == "timeout"


async def test_channel_reply_failure_timeout_and_cancel() -> None:
    handler = parse_hooks_option("{}")
    seen: list[dict[str, Any]] = []

    async def ok(params):
        seen.append(params)
        return {"decision": "allow"}

    handler.attach(ok)
    reply = await handler(_request(), CancelToken())
    assert reply.status == "reply" and reply.payload["result"] == {"decision": "allow"}
    assert seen[0]["sessionId"] == "s1" and seen[0]["requestId"] == "req-1#h1"

    async def boom(params):
        raise RuntimeError("x")

    handler.attach(boom)
    reply = await handler(_request(), CancelToken())
    assert reply.payload == {"id": "req-1#h1", "ok": False, "error": {"code": "host_failed"}}

    async def hang(params):
        await asyncio.Event().wait()

    handler.attach(hang)
    assert (await handler(_request(timeout_ms=50), CancelToken())).status == "timeout"
    token = CancelToken()
    asyncio.get_running_loop().call_later(0.05, token.cancel)
    assert (await handler(_request(timeout_ms=5000), token)).status == "cancelled"


class _Writer(AguiTransport):
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []

    async def send(self, frame: dict[str, Any]) -> bool:
        self.frames.append(frame)
        return True


async def test_agui_hook_frame_round_trip() -> None:
    handler = parse_hooks_option("{}")
    writer = _Writer()
    dispatcher = AguiAdapter(SimpleNamespace(hooks=handler), writer)
    pending = asyncio.create_task(handler(_request(), CancelToken()))
    while not writer.frames:
        await asyncio.sleep(0)
    frame = writer.frames[0]
    assert frame["kind"] == "hook" and frame["id"] == "req-1#h1" and frame["payload"]["point"] == "tool_pre"

    await dispatcher.handle("hook.reply", {"requestId": "req-1#h1", "result": {"decision": "deny"}}, "r2")
    reply = await pending
    assert reply.status == "reply" and reply.payload["result"] == {"decision": "deny"}
    assert writer.frames[-1] == {"v": 1, "id": "r2", "kind": "result", "payload": {"ok": True}}

    await dispatcher.handle("hook.reply", {"requestId": "req-1#h1", "result": {}}, "r3")
    assert writer.frames[-1]["kind"] == "error"
