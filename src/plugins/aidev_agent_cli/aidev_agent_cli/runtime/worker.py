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
from dataclasses import asdict, dataclass
from multiprocessing.connection import Connection
from typing import Any, Awaitable, Callable

from ..hooks.models import HookRequest
from ..bootstrap.network_guard import spawn_guard_descriptor
from ..bootstrap.startup import install_worker_startup
from ..utils.stdio import apply_worker_stdio_guard

#: 单帧 JSON 上限。父进程与子进程都按它切片，超出即拒绝。
MAX_FRAME_BYTES = 256 * 1024

#: 合作取消的等待上限（秒）。超时后进入 terminate → kill → join 的强制终止路径。
CANCEL_GRACE_SECONDS = 2.0

#: 子进程退出/强制终止各阶段的 join 等待上限（秒）。
JOIN_GRACE_SECONDS = 1.0

#: 收尾时等待后台回调结束的上限（秒）。
CALLBACK_CLEANUP_SECONDS = 0.1

__all__ = [
    "BACKGROUND_CALLBACKS",
    "CALLBACK_CLEANUP_SECONDS",
    "CANCEL_GRACE_SECONDS",
    "JOIN_GRACE_SECONDS",
    "MAX_FRAME_BYTES",
    "ProcessWorker",
    "WorkerContext",
    "WorkerReapError",
    "WorkerResult",
    "SDK_ENTRY",
    "stop_callback",
]

#: 跨 spawn 边界的**唯一**生产 import target。子进程 ``_worker_entry`` 完成全部保护之后才
#: ``import_module`` 到这里，执行体顶部的 SDK 依赖因此落在 guard / 环境 / OTel 之后就位
#: （见 ``sdk`` 包 docstring）。``ProcessWorker(entry=...)`` 只给测试替换。
SDK_ENTRY = "aidev_agent_cli.sdk.executor:build"

#: 构建阶段（含子进程装载执行体）的上限；超时按失败处理并强制终止。
BUILD_TIMEOUT_SECONDS = 180.0

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


def _check_target(target: str) -> tuple[str, str]:
    module, separator, name = target.partition(":")
    if not separator or not module or not name or "<locals>" in name:
        raise ValueError("worker factory must be an import target module:name")
    return module, name


def _encode_snapshot(snapshot: dict[str, Any]) -> bytes:
    if not isinstance(snapshot, dict):
        raise ValueError("worker snapshot must be a JSON object")
    data = json.dumps(snapshot, allow_nan=False).encode("utf-8")
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
    """worker 侧与父进程的 IPC 端点；按结构满足 ``sdk.spec.TurnPorts``（runtime 不 import sdk）。

    ``cancelled`` / ``hook`` / ``building`` 是单轮运行的端口，``emit`` 是本轮事件出口。
    """

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

    def building(self, build: Callable[[], Any]) -> Any:
        """构建 agent 期间上报 building（父进程据此计构建超时），结束后回到 running。"""
        self.send_frame({"kind": "phase", "value": "building"})
        try:
            return build()
        finally:
            self.send_frame({"kind": "phase", "value": "running"})

    def hook(self, **payload: Any) -> tuple[str, dict[str, Any] | None]:
        """一次宿主 hook 往返；``cancel_check`` 是 worker 本地回调，不跨进程。"""
        payload.pop("cancel_check", None)
        reply = self.request("hook", asdict(HookRequest(**payload)))
        return reply["status"], reply.get("payload")

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


def _snapshot_environment(snapshot: dict) -> tuple[dict[str, str], str]:
    """快照里的 ``process.env`` / ``cwd``；省略时沿用当前进程环境与 cwd。

    生产快照总是带着两者（父进程按配置解析好）；只关心 IPC 契约的测试可以不带。
    """
    settings = snapshot.get("process") if isinstance(snapshot, dict) else None
    settings = settings if isinstance(settings, dict) else {}

    env = settings.get("env")
    if env is None:
        env = dict(os.environ)
    elif not isinstance(env, dict):
        raise ValueError("worker snapshot env must be an object")

    cwd = settings.get("cwd")
    if cwd is None:
        cwd = os.getcwd()
    elif not isinstance(cwd, str) or not cwd:
        raise ValueError("worker snapshot cwd must be a non-empty string")

    return dict(env), cwd


def _worker_entry(connection: Connection, target: str, snapshot: bytes, guard_descriptor=None) -> None:
    """子进程入口：**先**装完全部保护，**再**装载 import target。

    顺序是契约，不是风格（``aidev_agent`` / ``ag_ui`` 在导入期就有副作用）：

    1. ``os.setsid`` —— 本轮独占进程组，父进程强杀时连后代一起收；
    2. ``install_worker_startup`` —— 快照 env 整体替换进程环境、切 cwd、登记出网 broker
       描述符，再按 guard → 环境 → 出网 → ``sys.path`` → OTel 装好全部保护；
    3. 只有到这里才 ``importlib.import_module`` —— 执行体顶部的 SDK 依赖随之就位。

    factory target 由父进程以 ``module:name`` 形式传入（生产为 ``SDK_ENTRY``）；本函数不做任何
    本包模块的 import，因此"worker 侧 SDK 何时进来"只由第 5 步决定。
    """
    os.setsid()
    executor = None
    context = WorkerContext(connection)
    try:
        frame = json.loads(snapshot)
        env, cwd = _snapshot_environment(frame)
        install_worker_startup(env, cwd, egress_fd=guard_descriptor)
        module, name = _check_target(target)
        factory = importlib.import_module(module)
        for component in name.split("."):
            factory = getattr(factory, component)
        # Worker traffic is IPC only, including native-library stdout and stdin reads：fd 重定向
        # 必须早于任何 SDK 调用（构造执行体）。SDK 已随 import target 进入 ``sys.modules``，
        # 导入期副作用可能改动进程级状态（fd / environ / socket / 模块属性），因此按同一顺序重放启动。
        apply_worker_stdio_guard()
        install_worker_startup(env, cwd)
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
    """One run, one spawned process. Returning always implies the child has been reaped.

    取消来源都在 ``run`` 的主循环里直接判定：``token``（调度侧取消）、构建超时、``cancel()``。
    """

    def __init__(
        self,
        snapshot: dict[str, Any],
        *,
        entry: str = SDK_ENTRY,
        token: Any = None,
        build_timeout: float | None = None,
        cancel_grace: float | None = None,
        join_grace: float | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.entry = entry
        self.token = token
        # 默认值在实例化时读取模块常量，便于测试整体收紧时限。
        self.build_timeout = BUILD_TIMEOUT_SECONDS if build_timeout is None else build_timeout
        self.cancel_grace = CANCEL_GRACE_SECONDS if cancel_grace is None else cancel_grace
        self.join_grace = JOIN_GRACE_SECONDS if join_grace is None else join_grace
        self.phase = "created"
        self.pid: int | None = None
        self.exitcode: int | None = None
        self.build_timed_out = False
        self._cancel_requested = False
        self._build_started: float | None = None
        self._started = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def _cancel_due(self) -> bool:
        token = self.token
        if token is not None and token.cancelled:
            if token.cancel_grace is not None:
                self.cancel_grace = min(self.cancel_grace, token.cancel_grace)
            return True
        if self._build_started is not None and asyncio.get_running_loop().time() - self._build_started >= self.build_timeout:
            self.build_timed_out = True
            return True
        return self._cancel_requested

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

    async def _reap(self, process: Any, grace_expired: bool = False) -> bool:
        forced = False
        # 合作取消宽限已耗尽时子进程已证明不响应，直接进入 SIGTERM，不再空等一轮自然退出。
        if not await self._join(process, 0 if grace_expired else self.join_grace):
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
        _check_target(self.entry)
        snapshot = _encode_snapshot(self.snapshot)
        self._build_started = asyncio.get_running_loop().time()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe(duplex=True)
        # Adopt orphaned descendants, but wait only for this worker's isolated group.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER (Linux)
            raise OSError(ctypes.get_errno(), "cannot enable controlled descendant reaping")
        process = context.Process(
            target=_worker_entry, args=(child, self.entry, snapshot, spawn_guard_descriptor()), daemon=False
        )
        result = None
        cancel_at = None
        saw_error = False
        callback: asyncio.Task[Any] | None = None
        pending_request = None
        forced = False
        grace_expired = False
        try:
            process.start()
            self.pid = process.pid
            child.close()
            while True:
                if cancel_at is None and self._cancel_due():
                    cancel_at = time.monotonic()
                    with contextlib.suppress(OSError):
                        _send(parent, {"kind": "cancel"})
                if cancel_at is not None and time.monotonic() - cancel_at >= self.cancel_grace:
                    result = WorkerResult(status="cancelled")
                    grace_expired = True
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
                        self._build_started = asyncio.get_running_loop().time() if self.phase == "building" else None
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
                    cleanup = asyncio.create_task(self._reap(process, grace_expired))
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