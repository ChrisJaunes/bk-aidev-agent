# -*- coding: utf-8 -*-
"""Runtime-internal run handles and cancellation tokens."""

from __future__ import annotations

import asyncio
import threading
from logging import getLogger
from typing import Any

from ..utils.runs import RunOutcome

logger = getLogger(__name__)

# ============================================================================
# §1  取消
# ============================================================================


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
# §2  句柄与结果
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