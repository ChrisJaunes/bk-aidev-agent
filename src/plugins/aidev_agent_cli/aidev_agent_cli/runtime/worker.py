"""Spawned run transport. Only import targets and JSON snapshots cross this boundary."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import importlib
import json
import multiprocessing
import os
import signal
import threading
import time
from dataclasses import dataclass
from multiprocessing.connection import Connection
from typing import Any, Awaitable, Callable

from .boundary import CALLBACK_CLEANUP_SECONDS, CANCEL_GRACE_SECONDS, MAX_FRAME_BYTES
from ..infrastructure.network_guard import install_from_env, spawn_guard_descriptor
from ..infrastructure.startup import (
    install_worker_startup,
    publish_environment,
    worker_cwd,
    worker_environment,
)

# ``boundary`` 是这些常量的定义处；本模块继续导出同样的名字（``runs`` / ``service`` 历史上
# 经本模块取 ``CANCEL_GRACE_SECONDS``，那条再导出链保持不变）。
__all__ = [
    "BACKGROUND_CALLBACKS",
    "CALLBACK_CLEANUP_SECONDS",
    "CANCEL_GRACE_SECONDS",
    "MAX_FRAME_BYTES",
    "ProcessWorker",
    "WorkerContext",
    "WorkerReapError",
    "WorkerResult",
    "WorkerSpec",
    "stop_callback",
]

BACKGROUND_CALLBACKS: set[asyncio.Task[Any]] = set()


def _callback_done(task: asyncio.Task[Any]) -> None:
    BACKGROUND_CALLBACKS.discard(task)
    if not task.cancelled():
        task.exception()


async def stop_callback(task: asyncio.Task[Any]) -> None:
    task.cancel()
    BACKGROUND_CALLBACKS.add(task)
    task.add_done_callback(_callback_done)
    await asyncio.wait({task}, timeout=CALLBACK_CLEANUP_SECONDS)



class WorkerReapError(RuntimeError):
    """A child is still alive; its slot and parent resources must remain held."""


def _send(connection: Connection, frame: dict[str, Any]) -> None:
    data = json.dumps(frame, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME_BYTES:
        raise ValueError("worker IPC frame exceeds limit")
    connection.send_bytes(data)


def _receive(connection: Connection) -> dict[str, Any]:
    frame = json.loads(connection.recv_bytes(MAX_FRAME_BYTES))
    if not isinstance(frame, dict) or not isinstance(frame.get("kind"), str):
        raise ValueError("invalid worker IPC envelope")
    return frame


@dataclass(frozen=True)
class WorkerSpec:
    factory: str
    snapshot: dict[str, Any]

    def encode(self) -> bytes:
        module, separator, name = self.factory.partition(":")
        if not separator or not module or not name or "<locals>" in name:
            raise ValueError("worker factory must be an import target module:name")
        if not isinstance(self.snapshot, dict):
            raise ValueError("worker snapshot must be a JSON object")
        data = json.dumps(self.snapshot, allow_nan=False).encode("utf-8")
        if len(data) > MAX_FRAME_BYTES:
            raise ValueError("worker snapshot exceeds limit")
        return data


@dataclass(frozen=True)
class WorkerResult:
    status: str
    value: Any = None
    error: str = ""
    forced: bool = False
    side_effects_warning: str = ""


class WorkerContext:
    """Worker-local event and request/reply endpoint; no parent objects are injected."""

    def __init__(self, connection: Connection) -> None:
        self._connection = connection
        self._cancelled = False
        self._request_id = 0
        self._send_lock = threading.Lock()
        self._receive_lock = threading.RLock()

    def send_frame(self, frame: dict[str, Any]) -> None:
        with self._send_lock:
            _send(self._connection, frame)

    def cancelled(self) -> bool:
        with self._receive_lock:
            while self._connection.poll():
                frame = _receive(self._connection)
                if frame["kind"] != "cancel":
                    raise ValueError("unexpected worker control frame")
                self._cancelled = True
            return self._cancelled

    def phase(self, phase: str) -> None:
        if phase not in ("building", "running"):
            raise ValueError("invalid worker phase")
        self.send_frame({"kind": "phase", "value": phase})

    def emit(self, event: Any) -> None:
        self.send_frame({"kind": "event", "value": event})

    def request(self, name: str, payload: Any) -> Any:
        # SDK hooks may run on producer threads. A reply must not be consumed by
        # a concurrent cancellation check; all frames share one writer as well.
        with self._receive_lock:
            self._request_id += 1
            request_id = self._request_id
            self.send_frame({"kind": "request", "id": request_id, "name": name, "value": payload})
            while True:
                frame = _receive(self._connection)
                if frame["kind"] == "cancel":
                    self._cancelled = True
                    continue
                if frame["kind"] != "reply" or frame.get("id") != request_id:
                    raise ValueError("unexpected worker reply")
                if frame.get("error"):
                    raise RuntimeError(frame["error"])
                return frame.get("value")


def _worker_entry(connection: Connection, target: str, snapshot: bytes, guard_descriptor=None) -> None:
    """子进程入口：**先**装完全部保护，**再**装载 import target。

    顺序是契约，不是风格（``aidev_agent`` / ``ag_ui`` 在导入期就有副作用）：

    1. ``os.setsid`` —— 本轮独占进程组，父进程强杀时连后代一起收；
    2. env 发布 —— ``snapshot["session"]["settings"]["env"]`` 原样进 ``os.environ``
       （``aidev_agent`` import 期要读自己的 settings）；
    3. 出网守卫 —— 评测 / 离线场景的 seccomp 与 broker 桥；
    4. ``install_worker_startup`` —— stdout guard / ``sys.path`` 加固 / OTel
       （``ag_ui`` 导入期的 print 不得落到 fd 1，OTel 必须在首次 SDK import 之前 patch）；
    5. 只有到这里才 ``importlib.import_module`` —— 执行体顶部的 SDK 依赖随之就位。

    factory target 由父进程以 ``module:name`` 形式写进 ``WorkerSpec``；本函数不做任何
    本包模块的 import，因此"worker 侧 SDK 何时进来"只由第 5 步决定。
    """
    os.setsid()
    executor = None
    context = WorkerContext(connection)
    try:
        frame = json.loads(snapshot)
        env = publish_environment(worker_environment(frame))
        os.chdir(worker_cwd(frame))
        if guard_descriptor is not None:
            os.environ["AGENT_TUI_EGRESS_FD"] = str(guard_descriptor)
            install_from_env()
        install_worker_startup(env)
        module, separator, name = target.partition(":")
        if not separator or not module or not name or "<locals>" in name:
            raise ValueError("worker factory must be an import target module:name")
        factory = importlib.import_module(module)
        for component in name.split("."):
            factory = getattr(factory, component)
        # ``building`` 表示"正在构建执行体"——它必须在 import target 之后：装载执行体的
        # 那段时间仍属于启动保护窗口，报成 building 会让父进程的构建超时把导入时间算进去。
        context.send_frame({"kind": "phase", "value": "building"})
        executor = factory(frame, context)
        context.send_frame({"kind": "phase", "value": "running"})
        value = executor.execute(context)
        context.send_frame(
            {"kind": "result", "status": "cancelled" if context.cancelled() else "finished", "value": value}
        )
    except BaseException as exc:
        with contextlib.suppress(OSError, ValueError):
            context.send_frame({"kind": "result", "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        try:
            if executor is not None:
                executor.close()
        finally:
            connection.close()


class ProcessWorker:
    """One run, one spawned process. Returning always implies the child has been reaped."""

    def __init__(
        self, spec: WorkerSpec, *, cancel_grace: float = CANCEL_GRACE_SECONDS, join_grace: float = 1.0
    ) -> None:
        self.spec = spec
        self.cancel_grace = cancel_grace
        self.join_grace = join_grace
        self.phase = "created"
        self.pid: int | None = None
        self.exitcode: int | None = None
        self._cancel = asyncio.Event()
        self._started = False

    def cancel(self) -> None:
        self._cancel.set()

    async def _join(self, process: Any, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while process.is_alive() and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        process.join(timeout=0)
        return not process.is_alive()

    def _signal_tree(self, process: Any, sig: int) -> None:
        try:
            if os.getpgid(process.pid) == process.pid:
                os.killpg(process.pid, sig)
            elif process.is_alive():
                os.kill(process.pid, sig)
        except ProcessLookupError:
            pass

    async def _reap(self, process: Any) -> bool:
        forced = False
        if not await self._join(process, self.join_grace):
            forced = True
            self._signal_tree(process, signal.SIGTERM)
            if not await self._join(process, self.join_grace):
                self._signal_tree(process, signal.SIGKILL)
                if not await self._join(process, self.join_grace):
                    raise WorkerReapError("worker could not be reaped; run slot must remain held")
        # This spawned worker exclusively owns its session, including descendants.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        deadline = time.monotonic() + self.join_grace
        while True:
            try:
                pid, _ = os.waitpid(-process.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                if time.monotonic() >= deadline:
                    raise WorkerReapError("worker descendants could not be reaped")
                await asyncio.sleep(0.005)
        self.exitcode = process.exitcode
        return forced

    async def run(
        self,
        *,
        event_sink: Callable[[Any], Awaitable[None]],
        request_handler: Callable[[str, Any], Awaitable[Any]],
        phase_handler: Callable[[str], None] | None = None,
    ) -> WorkerResult:
        if self._started:
            raise RuntimeError("worker is single-use")
        self._started = True
        snapshot = self.spec.encode()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        # Adopt orphaned descendants, but wait only for this worker's isolated group.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER (Linux)
            raise OSError(ctypes.get_errno(), "cannot enable controlled descendant reaping")
        process = context.Process(
            target=_worker_entry, args=(child, self.spec.factory, snapshot, spawn_guard_descriptor()), daemon=False
        )
        result = None
        cancel_at = None
        saw_error = False
        callback: asyncio.Task[Any] | None = None
        pending_request = None
        forced = False
        try:
            process.start()
            self.pid = process.pid
            child.close()
            while True:
                if self._cancel.is_set() and cancel_at is None:
                    cancel_at = time.monotonic()
                    with contextlib.suppress(OSError):
                        _send(parent, {"kind": "cancel"})
                if cancel_at is not None and time.monotonic() - cancel_at >= self.cancel_grace:
                    result = WorkerResult(status="cancelled")
                    break
                if callback is not None:
                    if callback.done():
                        value = callback.result()
                        callback = None
                        if pending_request is not None:
                            _send(parent, {"kind": "reply", "id": pending_request, "value": value})
                            pending_request = None
                    else:
                        await asyncio.sleep(0.005)
                        continue
                if parent.poll():
                    try:
                        frame = _receive(parent)
                    except EOFError:
                        if result is None:
                            result = WorkerResult(status="failed", error="worker exited without result")
                        break
                    kind = frame["kind"]
                    if kind == "event":
                        event = frame.get("value")
                        if isinstance(event, dict) and event.get("type") == "RUN_ERROR":
                            saw_error = True
                        callback = asyncio.create_task(event_sink(event))
                    elif kind == "request":
                        if not isinstance(frame.get("name"), str) or not isinstance(frame.get("id"), int):
                            raise ValueError("invalid worker request")
                        pending_request = frame["id"]
                        callback = asyncio.create_task(request_handler(frame["name"], frame.get("value")))
                    elif kind == "phase":
                        if frame.get("value") not in ("building", "running"):
                            raise ValueError("invalid worker phase")
                        self.phase = frame["value"]
                        if phase_handler is not None:
                            phase_handler(self.phase)
                    elif kind == "result":
                        status = frame.get("status")
                        if status not in ("finished", "cancelled", "failed"):
                            raise ValueError("invalid worker result")
                        if saw_error:
                            status = "failed"
                        if cancel_at is not None:
                            status = "cancelled"
                        result = WorkerResult(status=status, value=frame.get("value"), error=frame.get("error", ""))
                        break
                    else:
                        raise ValueError("unexpected worker frame")
                elif not process.is_alive():
                    result = WorkerResult(
                        status="cancelled" if cancel_at is not None else "failed", error="worker exited without result"
                    )
                    break
                await asyncio.sleep(0.005)
        finally:
            interrupted = False
            try:
                if process.pid is not None:
                    # Shield only reaping. Repeated parent cancellation must not interrupt kill/join.
                    cleanup = asyncio.create_task(self._reap(process))
                    while not cleanup.done():
                        try:
                            await asyncio.shield(cleanup)
                        except asyncio.CancelledError:
                            interrupted = True
                    forced = cleanup.result()
            finally:
                child.close()
                parent.close()
                if process.pid is not None and not process.is_alive():
                    process.close()
                    self.phase = "exited"
                if callback is not None:
                    await stop_callback(callback)
            if interrupted:
                raise asyncio.CancelledError
        assert result is not None
        if forced:
            return WorkerResult(
                status="cancelled" if cancel_at is not None else "failed",
                value=result.value,
                error=result.error or "worker required forced termination",
                forced=True,
                side_effects_warning="Forced termination cannot undo committed external side effects.",
            )
        if self.exitcode != 0:
            return WorkerResult(status="failed", error=f"worker exited with code {self.exitcode}")
        return result
