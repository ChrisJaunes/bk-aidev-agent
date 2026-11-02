"""Tests for the worker's stdout guard (``__main__.install_stdout_guard``).

WHY THIS FILE EXISTS
--------------------
`sys.stdout` is the worker's PROTOCOL channel: every frame the TUI reads is a
line of NDJSON on fd 1. Any stray `print()` from the SDK lands in that stream,
the TUI fails to parse it, and the user sees

    protocol error: ************...

which is noise with no functional effect but which has been reported for a long
time. The guard exists to prevent exactly that — and it had NO test coverage,
which is how it shipped unwired: `main()` never called it, while the module
docstring described it as step 1 of startup.
"""

from __future__ import annotations

import sys
from typing import Any

import pytest

from aidev_agent.headless import runtime as worker_main


@pytest.fixture(autouse=True)
def _reset_guard() -> Any:
    """Restore `sys.stdout` and the module globals around each test.

    The guard mutates process-global state, so a leaked install would corrupt
    every later test in the session.
    """
    saved_stdout = sys.stdout
    saved_installed = worker_main._installed
    saved_stream = worker_main._protocol_stream
    saved_real = worker_main._real_stdout

    yield

    sys.stdout = saved_stdout
    worker_main._installed = saved_installed
    worker_main._protocol_stream = saved_stream
    worker_main._real_stdout = saved_real


class _FakeStream:
    """A minimal text stream that records what it was given."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, data: str) -> int:
        self.chunks.append(data)

        return len(data)

    def flush(self) -> None:
        return None

    def fileno(self) -> int:
        return 2

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def test_guard_reroutes_print_to_stderr() -> None:
    """After installing, `print()` must NOT reach the protocol channel.

    `sys.stderr` is swapped BEFORE installing: the proxy captures whatever
    `sys.stderr` is at install time, so installing first would bind it to the
    real stderr and the assertion below would pass for the wrong reason.
    """
    stderr = _FakeStream()
    protocol = _FakeStream()

    original_stderr = sys.stderr
    sys.stderr = stderr  # type: ignore[assignment]
    try:
        worker_main.install_stdout_guard(protocol_stream=protocol)
        print("stray SDK output")
    finally:
        sys.stderr = original_stderr

    assert "stray SDK output" in stderr.text
    assert protocol.text == "", "protocol frames must be the only thing on the protocol stream"


def test_guard_is_what_main_installs() -> None:
    """`main()` must actually call the guard.

    THE REGRESSION. The guard was fully implemented and documented as startup
    step 1, but `main()` never called it — so `sys.stdout` stayed the real fd 1
    and every stray print reached the protocol stream. A guard that is never
    installed is indistinguishable from no guard at all.
    """
    import inspect

    source = inspect.getsource(worker_main.main)

    assert "install_stdout_guard()" in source, (
        "main() must install the stdout guard before anything can print; without it "
        "a stray SDK print becomes a protocol error in the TUI"
    )


def test_guard_is_idempotent() -> None:
    """A second install must not swap the proxy for the proxy."""
    protocol = _FakeStream()

    worker_main.install_stdout_guard(protocol_stream=protocol)
    after_first = sys.stdout

    worker_main.install_stdout_guard(protocol_stream=protocol)

    assert sys.stdout is after_first


def test_protocol_out_is_the_protocol_stream() -> None:
    """Frames must be written to the dedicated stream, never to `sys.stdout`."""
    protocol = _FakeStream()

    worker_main.install_stdout_guard(protocol_stream=protocol)

    assert worker_main.protocol_out() is protocol
    assert worker_main.protocol_out() is not sys.stdout


def test_captured_stdout_reroutes_within_the_block() -> None:
    """The second layer, for output produced while a run is in flight."""
    stderr = _FakeStream()
    original_stderr = sys.stderr
    sys.stderr = stderr  # type: ignore[assignment]
    try:
        with worker_main.captured_stdout():
            print("during a run")
    finally:
        sys.stderr = original_stderr

    assert "during a run" in stderr.text
