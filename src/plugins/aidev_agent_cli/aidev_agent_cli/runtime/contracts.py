# -*- coding: utf-8 -*-
"""Run requests, immutable outcomes, sinks and shielded result handles."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, Awaitable, Callable, Literal, Protocol, runtime_checkable
from uuid import uuid4

from .inputs import ChatInput, ContextInput, ResumeInterrupt

logger = getLogger(__name__)

# ============================================================================
# §1  终态
# ============================================================================

#: 终态取值。``rejected`` 不是「跑完之后的状态」，而是「这次 start 被拒绝」——
#: 它同样只落定一次，因此仍属终态的一种。
RunStatus = Literal["finished", "cancelled", "interrupted", "failed", "rejected"]


@dataclass(frozen=True)
class RunOutcome:
    """The single terminal outcome of a run; resource queries optionally carry data."""

    status: RunStatus
    run_id: str = ""
    session_code: str = ""
    error_code: str = ""
    error_message: str = ""
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    turn_id: str = ""
    data: Any = None

    @property
    def ok(self) -> bool:
        """这一轮是否「正常结束」（含中断轮——中断是结果，不是失败）。"""
        return self.status in ("finished", "interrupted")


# ============================================================================
# §2  事件出口
# ============================================================================


@runtime_checkable
class EventSink(Protocol):
    """Per-run asynchronous typed event destination."""

    async def send(self, event: Any) -> None: ...


# ============================================================================
# §3  取消
# ============================================================================


EventConsumer = EventSink | Callable[[dict[str, Any]], Awaitable[None]]


class CancelToken:
    """Cancellation signal; ProcessWorker owns cooperative grace and kill/join."""

    __slots__ = ("_event", "cancel_grace")

    def __init__(self) -> None:
        self._event = threading.Event()
        self.cancel_grace: float | None = None

    def cancel(self, *, grace: float | None = None) -> None:
        """Request cancellation; shutdown may shorten the cooperative deadline."""
        if grace is not None:
            self.cancel_grace = min(self.cancel_grace, grace) if self.cancel_grace is not None else grace
        self._event.set()

    def is_cancelled(self) -> bool:
        """供 backend 轮询的判定函数。"""
        return self._event.is_set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def __call__(self) -> bool:
        """让 token 本身可以当 ``is_cancelled`` 传进去（backend 的既有签名）。"""
        return self._event.is_set()


# ============================================================================
# §4  请求与会话选项
# ============================================================================


@dataclass(frozen=True)
class LaunchOptions:
    """命令行给出的进程级默认值（__main__ 解析，RuntimeService 消费）。

    三项都是**默认值**：会话请求里显式给出的值优先。
    """

    #: 默认模型（-m / --model）；空 = 由资源来源决定。
    model: str = ""
    #: 默认会话 code（-s / --session）；请求不带会话时使用。
    session: str = ""
    #: 叠加的资源来源（--resources，已展开）；空 = 协议默认值。
    resources: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionOptions:
    """Cross-turn configuration, without transport objects."""

    session_code: str
    agent_code: str = ""
    model: str = ""
    username: str = ""
    base_url: str = ""
    auth_headers: dict[str, str] | None = field(default=None, repr=False)
    temperature: float | None = None
    max_tokens: int | None = None
    resources: tuple[str, ...] = ()
    tool_factories: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunRequest:
    session_code: str
    input: ChatInput | ContextInput = field(default_factory=lambda: ChatInput(""))
    execute_options: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""


@dataclass(frozen=True)
class ResumeRequest:
    session_code: str
    input: ResumeInterrupt
    correlation_id: str = ""


# ============================================================================
# §5  句柄与结果
# ============================================================================


class RunHandle:
    """Shielded shared result, settled exactly once by the scheduler after worker join."""

    __slots__ = ("run_id", "session_code", "_token", "_task", "_future", "_settled", "_phase")

    def __init__(self, *, run_id: str, session_code: str, token: CancelToken) -> None:
        self.run_id = run_id
        self.session_code = session_code
        self._token = token
        self._task: asyncio.Task[Any] | None = None
        self._future: asyncio.Future[RunOutcome] = asyncio.get_running_loop().create_future()
        self._settled = False
        self._phase = "queued"

    # ------------------------------------------------------------------ 只读

    @property
    def token(self) -> CancelToken:
        return self._token

    @property
    def task(self) -> asyncio.Task[Any] | None:
        return self._task

    @property
    def phase(self) -> str:
        return self._phase

    def mark_phase(self, phase: str) -> None:
        if not self._settled:
            self._phase = phase

    @property
    def settled(self) -> bool:
        return self._settled

    # ------------------------------------------------------------------ 生命周期

    def attach_task(self, task: asyncio.Task[Any]) -> None:
        self._task = task

    def settle(self, outcome: RunOutcome) -> None:
        """落定终态。**唯一**入口；第二次调用只记日志、不覆盖。"""
        if self._settled:
            logger.info(f"run {self.run_id} settled twice; ignoring the second outcome ({outcome.status})")

            return

        self._settled = True
        self._phase = outcome.status
        if not self._future.done():
            self._future.set_result(outcome)

    async def result(self) -> RunOutcome:
        """等待并返回终态（重复 await 返回同一个对象）。"""
        return await asyncio.shield(self._future)


def new_session_code() -> str:
    """Create a new session identity."""
    return f"thread-{uuid4().hex[:12]}"


__all__ = [
    "CancelToken",
    "EventSink",
    "LaunchOptions",
    "ResumeRequest",
    "RunHandle",
    "RunOutcome",
    "RunRequest",
    "RunStatus",
    "SessionOptions",
    "new_session_code",
]
