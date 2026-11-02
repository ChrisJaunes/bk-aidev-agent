"""Tests for :mod:`worker.frame_protocol` — the serialized NDJSON writer (D-07)."""

from __future__ import annotations

import asyncio
import errno
import json
from typing import Any

import pytest

from aidev_agent.headless.frame_protocol import _QUEUE_MAX, FrameWriter


class _BrokenPipeStream:
    """A stream whose every write raises ``BrokenPipeError``."""

    def __init__(self) -> None:
        self.writes = 0

    def write(self, line: str) -> int:
        self.writes += 1
        raise BrokenPipeError("simulated parent gone")

    def flush(self) -> None:
        return None


async def test_concurrent_writes_never_interleave() -> None:
    """50 concurrent senders, each with a distinct multi-byte payload.

    This is the D-07 regression test: without the single-consumer queue two
    JSON lines can splice mid-line and the output stops being line-delimited
    JSON.
    """
    import io

    stream = io.StringIO()
    writer = FrameWriter(stream, maxsize=_QUEUE_MAX)
    await writer.start()

    n = 50
    # A multi-byte Chinese string forces ensure_ascii=False behaviour and makes
    # any byte-level interleaving obvious.
    payload_zh = "你好，世界！这是一段足够长的中文内容，用来放大潜在的交错问题。" * 4

    async def sender(i: int) -> None:
        await writer.send({"v": 1, "id": f"req-{i}", "kind": "event", "payload": {"delta": payload_zh, "seq": i}})

    await asyncio.gather(*(sender(i) for i in range(n)))
    await writer.close()

    lines = [ln for ln in stream.getvalue().split("\n") if ln]
    assert len(lines) == n, f"expected {n} lines, got {len(lines)}"

    seen_ids: set[str] = set()
    for line in lines:
        parsed: dict[str, Any] = json.loads(line)  # each line must parse alone
        seen_ids.add(parsed["id"])
        assert parsed["payload"]["delta"] == payload_zh
    assert seen_ids == {f"req-{i}" for i in range(n)}
    assert not writer.peer_gone


async def test_peer_gone_on_broken_pipe() -> None:
    """A write that raises BrokenPipeError flags peer-gone without raising."""
    stream = _BrokenPipeStream()
    writer = FrameWriter(stream)
    await writer.start()

    assert await writer.send({"v": 1, "id": "x", "kind": "ready"}) is True

    # Let the drain task observe the broken pipe.
    for _ in range(50):
        if writer.peer_gone:
            break
        await asyncio.sleep(0.01)

    assert writer.peer_gone is True

    # Subsequent sends short-circuit to False and never raise.
    assert await writer.send({"v": 1, "id": "y", "kind": "ready"}) is False

    await writer.close()  # must not raise


async def test_serialization_error_reraises() -> None:
    """A non-serializable payload is a programming error, not a disconnect."""
    import io

    stream = io.StringIO()
    writer = FrameWriter(stream)
    await writer.start()

    with pytest.raises(TypeError):
        await writer.send({"v": 1, "id": "bad", "bad": object()})

    # The bug must NOT have been disguised as a clean peer-gone.
    assert writer.peer_gone is False

    await writer.close()


async def test_bounded_queue() -> None:
    """The queue is bounded so a huge tool output applies back-pressure."""
    import io

    writer = FrameWriter(io.StringIO())
    assert writer.queue.maxsize == 256
    assert _QUEUE_MAX == 256
    await writer.start()
    await writer.close()


# ---------------------------------------------------------------- drain thread
#
# WHY THE DRAIN MUST NOT RUN ON THE EVENT LOOP (the hang this pins)
# -----------------------------------------------------------------
# The drain used to be an `asyncio` task doing `stream.write()` + `flush()`
# inline. `write()` to a pipe is a SYNCHRONOUS syscall with a 64 KiB kernel
# buffer, so the moment node lagged for an instant the write blocked INSIDE the
# event loop. The drain could then never return to `queue.get()`, the producer's
# `await queue.put()` could never be woken, and the whole turn hung — the TUI sat
# on "streaming…" forever and the typewriter effect vanished, because whole bursts
# were flushed at once instead of a delta at a time.
#
# Measured: an `asyncio.sleep(0.01)` heartbeat executed ZERO times for the entire
# duration of a stall.
#
# These tests use a REAL pipe whose read end nobody drains, which is the only way
# to make `write()` block for real.


async def test_the_event_loop_keeps_running_while_the_drain_is_blocked() -> None:
    """A full pipe must block the drain THREAD, never the event loop.

    This is the regression test for the hang: if the drain goes back to being an
    asyncio task, the heartbeat stops and this fails.
    """
    import os

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8", errors="replace")
    writer = FrameWriter(stream)

    try:
        await writer.start()

        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        hb = asyncio.create_task(heartbeat())

        async def flood() -> None:
            # ~400 bytes a frame, so a few hundred frames overflow both the
            # 256-slot queue and the 64 KiB pipe buffer.
            for i in range(2000):
                if not await writer.send(
                    {"v": 1, "id": f"f{i}", "kind": "event", "payload": {"d": "x" * 400}}
                ):
                    return

        try:
            await asyncio.wait_for(flood(), timeout=5)
        except asyncio.TimeoutError:
            # Expected: nobody is reading the pipe, so back-pressure is correct.
            pass

        hb.cancel()

        # With the old (event-loop) drain this was 0 — the loop was wedged.
        assert ticks > 50, (
            f"event loop was blocked while the drain stalled (ticks={ticks}); "
            "the drain has probably been moved back onto the event loop"
        )
    finally:
        os.close(read_fd)
        try:
            stream.close()
        except BrokenPipeError:
            # The pipe's read end is gone by design; closing our end is allowed
            # to complain and is not what this test asserts.
            pass


async def test_a_wedged_drain_does_not_hang_the_sender_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """`send` gives up rather than blocking a turn indefinitely.

    Bounded back-pressure. The old code awaited `queue.put()` with no deadline,
    which is what turned "node is briefly slow" into "the session is over".
    """
    import os

    import aidev_agent.headless.frame_protocol as writer_mod

    monkeypatch.setattr(writer_mod, "_ENQUEUE_TIMEOUT_SECONDS", 0.2)

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8", errors="replace")
    writer = FrameWriter(stream)

    try:
        await writer.start()

        async def flood() -> bool:
            for i in range(5000):
                if not await writer.send({"v": 1, "id": f"f{i}", "kind": "event", "payload": {"d": "x" * 400}}):
                    return True
            return False

        gave_up = await asyncio.wait_for(flood(), timeout=15)

        assert gave_up, "send never gave up on a wedged drain — the turn would hang"
    finally:
        os.close(read_fd)
        try:
            stream.close()
        except BrokenPipeError:
            pass


async def test_peer_gone_returns_false_and_is_not_an_exception() -> None:
    """A closed read end is an ordinary outcome, not a traceback.

    Preserved from the original contract: a clean disconnect must stay
    distinguishable from a real I/O bug.
    """
    import os

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(write_fd, "w", buffering=1, encoding="utf-8", errors="replace")
    writer = FrameWriter(stream)

    try:
        await writer.start()
        # Close the READ end: the next flush raises EPIPE.
        os.close(read_fd)
        read_fd = -1

        for i in range(50):
            if not await writer.send({"v": 1, "id": f"f{i}", "kind": "event", "payload": {}}):
                break
        else:
            # Give the drain thread a moment to actually hit EPIPE.
            await asyncio.sleep(0.3)
            for i in range(50, 300):
                if not await writer.send({"v": 1, "id": f"f{i}", "kind": "event", "payload": {}}):
                    break

        assert writer.peer_gone is True, "a broken pipe did not mark the writer dead"
        # A disconnect is NOT a failure: close() must not raise for it.
        assert writer.failure is None
    finally:
        if read_fd != -1:
            os.close(read_fd)
        try:
            stream.close()
        except BrokenPipeError:
            pass


async def test_close_re_raises_a_fatal_drain_failure() -> None:
    """A non-peer-gone write error must reach the log, not die on the thread.

    The drain has no caller to raise into any more, so `close()` is the delivery
    path. Without this the traceback would vanish and a real bug would look like
    a quiet disconnect.
    """

    class _HostProblem:
        """Fails with ENOSPC — a host problem, explicitly not a disconnect."""

        def write(self, _line: str) -> None:
            raise OSError(errno.ENOSPC, "no space left on device")

        def flush(self) -> None:
            return None

    writer = FrameWriter(_HostProblem())
    await writer.start()

    # Enough to get the frame to the drain.
    await writer.send({"v": 1, "id": "x", "kind": "event", "payload": {}})
    await asyncio.sleep(0.2)

    assert writer.failure is not None, "a fatal OSError was swallowed"

    with pytest.raises(OSError):
        await writer.close()
