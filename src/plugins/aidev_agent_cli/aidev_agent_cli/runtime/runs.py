"""Explicit per-session FIFO slots, queued admission, and bounded completed results."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from uuid import uuid4

from .contracts import CancelToken, EventConsumer, ResumeRequest, RunHandle, RunOutcome, RunRequest
from .models import HookHandler, TurnSnapshot
from .runner import SDKRunner
from .session import SessionRegistry
from .worker import CANCEL_GRACE_SECONDS, WorkerReapError


@dataclass
class CancelOutcome:
    session_code: str
    cancelled: bool
    run_id: str = ""
    reason: str = ""
    run_ids: list[str] = field(default_factory=list)


@dataclass
class Admission:
    handle: RunHandle
    snapshot: TurnSnapshot
    sink: EventConsumer | None
    hook_handler: HookHandler | None


@dataclass
class Slot:
    active: Admission | None = None
    queue: deque[Admission] = field(default_factory=deque)


class FIFOScheduler:
    def __init__(
        self, sessions: SessionRegistry, *, busy_mode: str = "reject", completed_limit: int = 128, on_reap_failure=None
    ):
        self._sessions = sessions
        self._runner = SDKRunner()
        self._busy_mode = busy_mode
        self._closing = False
        self._slots: dict[str, Slot] = {}
        self._completed: OrderedDict[str, RunHandle] = OrderedDict()
        self._completed_limit = completed_limit
        self._unreaped: set[str] = set()
        self._on_reap_failure = on_reap_failure

    def handles(self):
        result = {}
        for handle in self._completed.values():
            result.setdefault(handle.session_code, {})[handle.run_id] = handle
        for code in self._slots:
            result.setdefault(code, {}).update(self.live_runs(code))
        return result

    def live_runs(self, session_code):
        slot = self._slots.get(session_code)
        if slot is None:
            return {}
        admissions = ([slot.active] if slot.active else []) + list(slot.queue)
        return {item.handle.run_id: item.handle for item in admissions if not item.handle.settled}

    def busy(self, session_code):
        slot = self._slots.get(session_code)
        return bool(slot and (slot.active or slot.queue))

    async def submit(
        self,
        request: RunRequest | ResumeRequest,
        *,
        snapshot: TurnSnapshot,
        sink: EventConsumer | None = None,
        hook_handler: HookHandler | None = None,
    ) -> RunHandle:
        handle = RunHandle(run_id=f"run-{uuid4().hex}", session_code=request.session_code, token=CancelToken())
        info = self._sessions.info(request.session_code)
        code = "runtime.closing" if self._closing else ""
        if not code and (info is None or info.closed):
            code = "session.not_found"
        if not code and self.busy(request.session_code) and self._busy_mode == "reject":
            code = "session.busy"
        if code:
            self._complete(
                handle, RunOutcome("rejected", handle.run_id, handle.session_code, error_code=code, error_message=code)
            )
            return handle
        item = Admission(handle, snapshot, sink, hook_handler)
        slot = self._slots.setdefault(request.session_code, Slot())
        slot.queue.append(item)
        self._advance(request.session_code)
        return handle

    def _advance(self, code):
        slot = self._slots[code]
        if slot.active is not None or not slot.queue or code in self._unreaped:
            return
        item = slot.queue.popleft()
        slot.active = item
        item.handle.mark_phase("starting")
        item.handle.attach_task(asyncio.create_task(self._drive(item), name=item.handle.run_id))

    async def _drive(self, item):
        handle = item.handle
        release = True
        try:
            if handle.token.cancelled:
                outcome = RunOutcome("cancelled", handle.run_id, handle.session_code)
            else:
                outcome = await self._runner.execute(
                    handle=handle,
                    session=self._sessions.get(handle.session_code),
                    sink=item.sink,
                    hook_handler=item.hook_handler,
                    snapshot=item.snapshot,
                )
        except BaseException as exc:
            if isinstance(exc, WorkerReapError):
                release = False
                self._unreaped.add(handle.session_code)
                self._closing = True
                if self._on_reap_failure is not None:
                    self._on_reap_failure()
            outcome = RunOutcome(
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
                handle.run_id,
                handle.session_code,
                error_code=getattr(exc, "code", "agent.internal_error"),
                error_message=getattr(exc, "message", type(exc).__name__),
            )
        finally:
            self._complete(handle, outcome)
            if release:
                self._slots[handle.session_code].active = None
                self._advance(handle.session_code)

    def _complete(self, handle, outcome):
        handle.settle(outcome)
        self._completed[handle.run_id] = handle
        while len(self._completed) > self._completed_limit:
            self._completed.popitem(last=False)

    async def cancel(self, session_code, run_id=None, *, grace=None):
        targets = self.live_runs(session_code)
        if run_id is not None:
            targets = {run_id: targets[run_id]} if run_id in targets else {}
        slot = self._slots.get(session_code)
        for handle in targets.values():
            handle.token.cancel(grace=grace)
        if slot:
            remaining = deque()
            for item in slot.queue:
                if item.handle.run_id in targets:
                    self._complete(item.handle, RunOutcome("cancelled", item.handle.run_id, session_code))
                else:
                    remaining.append(item)
            slot.queue = remaining
        return CancelOutcome(
            session_code, bool(targets), run_id or "", "" if targets else "no active run", list(targets)
        )

    async def close_session(self, code, *, grace=None):
        await self.cancel(code, grace=grace)
        slot = self._slots.get(code)
        if slot and slot.active and slot.active.handle.task:
            await asyncio.shield(slot.active.handle.task)
        if code in self._unreaped:
            raise RuntimeError("worker reaping failed; runtime resources and slot remain held")
        self._slots.pop(code, None)

    def begin_close(self):
        self._closing = True

    async def close_all(self, reason, *, grace=None):
        self.begin_close()
        await asyncio.gather(
            *(
                self.close_session(code, grace=CANCEL_GRACE_SECONDS if grace is None else grace)
                for code in list(self._slots)
            )
        )
        if self._unreaped:
            raise RuntimeError("worker reaping failed; runtime resources remain held")
