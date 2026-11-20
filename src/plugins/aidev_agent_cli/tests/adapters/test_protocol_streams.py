"""``adapters.streams`` 的背压、故障唤醒、写出确认与限长行为（主测 ``adapters``）。

含一条 SIGTERM 子进程回归（``install_stdout_guard`` / ``protocol_out`` / ``LineWriter``）。
协议 adapter 在输出侧故障时的收尾（``AcpTransport`` / ``run_agui``）已按断言目标拆到
``tests/adapters/test_protocol_streams_adapters.py``。
"""

from __future__ import annotations

import asyncio
import errno
import io
import sys
import threading

import pytest

from aidev_agent_cli.adapters.streams import LineReader, LineWriter, ProtocolChannel, until_stopped


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


async def test_write_ack_waits_without_blocking_event_loop() -> None:
    stream = SlowStream()
    writer = LineWriter(stream)
    await writer.start()
    sending = asyncio.create_task(writer.write("frame\n", wait_written=True))
    try:
        for _ in range(100):
            if stream.entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert stream.entered.is_set()
        await asyncio.sleep(0.01)
        assert not sending.done()
        stream.release.set()
        assert await asyncio.wait_for(sending, 1)
    finally:
        stream.release.set()
        await writer.close()
    assert stream.lines == ["frame\n"]


async def test_single_stalled_frame_stops_writer_and_wakes_waiters() -> None:
    stream = SlowStream()
    writer = LineWriter(stream, write_timeout=0.03)
    await writer.start()
    try:
        assert not await asyncio.wait_for(writer.write("frame\n", wait_written=True), 1)
        assert writer.stopped.is_set()
        assert isinstance(writer.failure, TimeoutError)
        assert not await writer.write("later\n")
    finally:
        stream.release.set()
        with pytest.raises(TimeoutError):
            await writer.close()


async def test_close_has_deadline_and_does_not_close_owned_stream() -> None:
    stream = SlowStream()
    writer = LineWriter(stream)
    await writer.start()
    assert await writer.write("frame\n")
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(writer.close(timeout=0.03), 1)
        assert writer.stopped.is_set()
    finally:
        stream.release.set()


@pytest.mark.parametrize(
    "error", [BrokenPipeError(), OSError(errno.ENOSPC, "full"), UnicodeEncodeError("ascii", "中", 0, 1, "unsupported")]
)
async def test_write_errors_wake_receive_and_preserve_fatal_error(error: Exception) -> None:
    class FailedStream:
        def write(self, _: str) -> int:
            raise error

    writer = LineWriter(FailedStream())
    await writer.start()
    waiting = asyncio.create_task(until_stopped(asyncio.Event().wait(), writer.stopped))
    assert not await writer.write("frame\n", wait_written=True)
    assert await asyncio.wait_for(waiting, 1) is None
    if isinstance(error, BrokenPipeError):
        assert writer.failure is None
        await writer.close()
    else:
        assert writer.failure is error
        with pytest.raises(type(error)):
            await writer.close()


async def test_reader_discards_oversized_line_and_recovers() -> None:
    drops: list[bool] = []
    reader = LineReader(io.BytesIO("中中\nok\nlast".encode()), limit=5, on_oversized=lambda: drops.append(True))
    try:
        assert await reader.receive() == b"ok"
        assert await reader.receive() == b"last"
        assert await reader.receive() is None
        assert drops == [True]
    finally:
        reader.close()


async def test_reader_bounds_unterminated_line_and_preserves_split_utf8() -> None:
    class Chunks:
        def __init__(self) -> None:
            self.parts = iter([b"x" * 6, b"x" * 6, b"\n\xe4", b"\xb8\xad\n", b"tail", b""])

        def read(self, _: int) -> bytes:
            return next(self.parts)

    reader = LineReader(Chunks(), limit=5, eof_line=False)
    try:
        assert await reader.receive() == "中".encode()
        assert len(reader._buffer) <= 5
        assert await reader.receive() is None
    finally:
        reader.close()


async def test_concurrent_acknowledged_writes_are_serial_and_drained() -> None:
    stream = io.StringIO()
    writer = LineWriter(stream, maxsize=2)
    await writer.start()
    assert all(await asyncio.gather(*(writer.write(f"{i}\n", wait_written=True) for i in range(100))))
    await writer.close()
    assert stream.getvalue().splitlines() == [str(i) for i in range(100)]
    assert not writer._pending


async def test_close_wakes_backpressured_producer() -> None:
    stream = SlowStream()
    writer = LineWriter(stream, maxsize=1)
    await writer.start()
    try:
        assert await writer.write("active\n")
        for _ in range(100):
            if stream.entered.is_set():
                break
            await asyncio.sleep(0.001)
        assert stream.entered.is_set()
        assert await writer.write("queued\n")
        producer = asyncio.create_task(writer.write("waiting\n"))
        await asyncio.sleep(0.01)
        assert not producer.done()
        with pytest.raises(TimeoutError):
            await writer.close(timeout=0.03)
        assert not await asyncio.wait_for(producer, 1)
    finally:
        stream.release.set()


async def test_cancelling_close_settles_ack_and_stops_accepting() -> None:
    stream = SlowStream()
    writer = LineWriter(stream)
    await writer.start()
    sending = asyncio.create_task(writer.write("active\n", wait_written=True))
    try:
        await asyncio.sleep(0.01)
        closing = asyncio.create_task(writer.close())
        await asyncio.sleep(0.01)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        assert not await asyncio.wait_for(sending, 1)
        assert not await writer.write("later\n")
    finally:
        stream.release.set()


async def test_protocol_channel_has_one_read_write_contract() -> None:
    """所有协议共用同一套语义：超长行丢弃、EOF 残留行交付、发送确认写出。"""
    output = io.StringIO()
    channel = ProtocolChannel(io.BytesIO(b"x" * 10 + b"\nok\nlast"), output, limit=5)
    assert await channel.receive() == b"ok"
    assert await channel.receive() == b"last"
    assert await channel.receive() is None
    assert await channel.send("frame\n", wait_written=True) is True
    assert output.getvalue() == "frame\n"
    await channel.close()
    assert await channel.send("late\n") is False
    assert await channel.receive() is None


async def test_protocol_channel_output_failure_ends_receive() -> None:
    release = threading.Event()

    class OpenInput:
        def read(self, _: int) -> bytes:
            release.wait()
            return b""

    class BrokenOutput:
        def write(self, _: str) -> int:
            raise BrokenPipeError()

    channel = ProtocolChannel(OpenInput(), BrokenOutput())
    receiving = asyncio.create_task(channel.receive())
    try:
        assert await channel.send("x\n", wait_written=True) is False
        assert channel.peer_gone
        assert await asyncio.wait_for(receiving, 1) is None
    finally:
        release.set()
        await channel.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal and pipe regression")
def test_stalled_output_process_exits_without_buffer_lock_abort() -> None:
    import selectors
    import subprocess

    code = """
import asyncio, contextlib, signal, sys
from aidev_agent_cli.utils.stdio import install_stdout_guard, protocol_out
from aidev_agent_cli.adapters.streams import LineWriter
async def main():
    install_stdout_guard()
    writer = LineWriter(protocol_out())
    await writer.start()
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    await writer.write("x" * 1000000)
    print("ready", file=sys.stderr, flush=True)
    await stop.wait()
    with contextlib.suppress(TimeoutError):
        await writer.close(timeout=0.05)
asyncio.run(main())
"""
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr, selectors.EVENT_READ)
            assert selector.select(timeout=5), "child did not become ready"
        assert process.stderr.readline() == b"ready\n"
        process.terminate()
        assert process.wait(timeout=5) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()