# -*- coding: utf-8 -*-
"""协议 adapter 的流 I/O 故障行为（主测 ``adapters``）。

从 ``tests/adapters/test_protocol_streams.py`` 按断言目标拆出：那一侧只留
``adapters.streams.LineReader`` / ``LineWriter`` / ``until_stopped`` 的背压、限长与
写出确认单元测试（含 SIGTERM 子进程回归）；本文件承载协议 adapter 在**输出侧故障**时
如何收尾——``adapters.acp.AcpTransport`` 的 pending/断管与 ``adapters.agui.run_agui``
的空闲输入唤醒 + service 关闭。
"""

from __future__ import annotations

import asyncio
import io
import threading
from types import SimpleNamespace

import pytest

from aidev_agent_cli.adapters.streams import ProtocolChannel


class SlowStream:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.lines: list[str] = []

    def write(self, line: str) -> int:
        self.entered.set()
        self.release.wait()
        self.lines.append(line)
        return len(line)

    def flush(self) -> None:
        pass


async def test_acp_pending_is_settled_only_after_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    from aidev_agent_cli.adapters.acp import AcpTransport

    stream = SlowStream()
    transport = AcpTransport(ProtocolChannel(io.BytesIO(), stream))
    transport._pending_requests.add(1)
    sending = asyncio.create_task(transport.send({"id": 1, "result": {}}))
    try:
        await asyncio.sleep(0.02)
        assert transport._pending_requests == {1}
        assert not sending.done()
        stream.release.set()
        await asyncio.wait_for(sending, 1)
        assert transport._pending_requests == set()
    finally:
        stream.release.set()
        await transport.close()


async def test_acp_output_failure_exits_with_stdin_still_open(monkeypatch: pytest.MonkeyPatch) -> None:
    from aidev_agent_cli.adapters.acp import AcpTransport

    release = threading.Event()

    class OpenInput:
        def read(self, _: int) -> bytes:
            release.wait()
            return b""

    class BrokenOutput:
        def write(self, _: str) -> int:
            raise BrokenPipeError()

    transport = AcpTransport(ProtocolChannel(OpenInput(), BrokenOutput()))
    receiving = asyncio.create_task(transport.receive())
    try:
        await asyncio.sleep(0.01)
        with pytest.raises(ConnectionError):
            await transport.send({"id": 1, "result": {}})
        assert await asyncio.wait_for(receiving, 1) is None
    finally:
        release.set()
        await transport.close()


async def test_agui_output_failure_wakes_idle_input_and_closes_service(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    from aidev_agent_cli.adapters import agui

    release = threading.Event()

    class OpenInput:
        def read(self, _: int) -> bytes:
            release.wait()
            return b""

    class BrokenOutput:
        def write(self, _: str) -> int:
            raise BrokenPipeError()

    monkeypatch.setattr(agui, "async_shutdown_event", asyncio.Event)
    monkeypatch.setattr(agui, "use_daemon_default_executor", lambda *_: None)
    service = SimpleNamespace(close_all=AsyncMock())
    try:
        assert await asyncio.wait_for(agui.run_agui(service, ProtocolChannel(OpenInput(), BrokenOutput())), 1) == 0
        service.close_all.assert_awaited_once()
        assert not any(task.get_name() in {"agui-shutdown", "agui-output-stop"} for task in asyncio.all_tasks())
    finally:
        release.set()