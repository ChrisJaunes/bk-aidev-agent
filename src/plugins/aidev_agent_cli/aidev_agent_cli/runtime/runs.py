"""Per-session FIFO admission and the single parent-side turn: spawn, join, finalize."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass, field, replace
from functools import partial
from typing import Any
from uuid import uuid4

from ..hooks.models import HookReply, HookRequest, ProcessHooks
from .handles import CancelToken, RunHandle
from ..infrastructure.ports import EventConsumer
from ..utils.runs import ResumeRequest, RunOutcome, RunRequest, SessionState
from ..utils.history import LocalHistory
from .models import TurnSnapshot
from .session import Session, SessionRegistry
from ..utils.terminal import build_cancel_terminal_events, build_error_event
from .worker import CANCEL_GRACE_SECONDS, ProcessWorker, WorkerReapError, stop_callback

TERMINAL_DELIVERY_SECONDS = 1.0

#: 只有这些结局的轮次提交会话状态；失败 / 取消 / 强杀整体回滚（含 turn_count）。
COMMITTED_STATUSES = ("finished", "interrupted")


def _record_run(
    history_path: str,
    run_id: str,
    session_code: str,
    status: str,
    *,
    forced: bool = False,
    state: SessionState | None = None,
):
    history = LocalHistory(history_path)
    try:
        if state is not None:
            history.save(session_code, asdict(state))
        history.record_run(run_id, session_code, status, forced=forced)
    finally:
        history.close()


def _committed_state(kind: str, result: Any) -> SessionState | None:
    """worker 传回的合并后状态；仅 start / resume 且本轮正常落定时返回，否则 ``None``（不提交）。"""
    if kind not in ("start", "resume") or result.forced or result.status != "finished":
        return None
    value = result.value if isinstance(result.value, dict) else {}
    if value.get("outcome", {}).get("status") not in COMMITTED_STATUSES or "state" not in value:
        return None
    return SessionState.from_dict(value["state"])


async def _call_sink(sink: EventConsumer, event: dict[str, Any]) -> None:
    """Deliver one typed event to the run's asynchronous destination."""
    send = getattr(sink, "send", None)
    await (send(event) if send is not None else sink(event))


async def execute_turn(
    *,
    handle: RunHandle,
    session: Session,
    sink: EventConsumer | None,
    snapshot: TurnSnapshot,
    history_path: str,
    hooks: ProcessHooks | None = None,
) -> RunOutcome:
    """One turn owns one ProcessWorker; no SDK objects cross the spawn boundary.

    ``hooks`` 是进程级的：worker 发来的 hook 请求一律交给它应答（module 模式下 worker 不会发）。
    """
    kind = snapshot.kind
    session.ensure_open()
    if handle.token.cancelled:
        return RunOutcome(status="cancelled", run_id=handle.run_id, session_code=handle.session_code)
    snapshot = replace(snapshot, run_id=handle.run_id, correlation_id=snapshot.correlation_id or handle.run_id)
    if snapshot.rejection is not None:
        code, message = snapshot.rejection["code"], snapshot.rejection["message"]
        await asyncio.to_thread(_record_run, history_path, handle.run_id, handle.session_code, "failed")
        if sink is not None:
            await _call_sink(sink, build_error_event(message))
        return RunOutcome("failed", handle.run_id, handle.session_code, error_code=code, error_message=message)
    await asyncio.to_thread(_record_run, history_path, handle.run_id, handle.session_code, "building")
    worker = ProcessWorker(snapshot.payload(), token=handle.token)
    terminal_events = []
    finalizing = False

    async def deliver(event: Any) -> None:
        if isinstance(event, dict) and event.get("type") in ("RUN_ERROR", "RUN_FINISHED") and not finalizing:
            terminal_events.append(event)
            return
        if sink is not None:
            await _call_sink(sink, event)

    async def hook(name: str, payload: Any) -> Any:
        if name != "hook" or hooks is None:
            return asdict(HookReply("timeout"))
        return asdict(await hooks(HookRequest(**payload), handle.token))

    result = await worker.run(event_sink=deliver, request_handler=hook, phase_handler=handle.mark_phase)
    if worker.build_timed_out:
        result = replace(result, status="failed", error="agent build timed out")

    persisted_status = result.status
    if result.status == "finished" and isinstance(result.value, dict):
        persisted_status = result.value.get("outcome", {}).get("status", persisted_status)
    state = _committed_state(kind, result)
    await asyncio.to_thread(
        _record_run, history_path, handle.run_id, handle.session_code, persisted_status, forced=result.forced, state=state
    )
    if state is not None:
        session.state = state
    finalizing = True
    transport_error = ""
    terminal_deadline = asyncio.get_running_loop().time() + TERMINAL_DELIVERY_SECONDS

    async def terminal(event):
        nonlocal transport_error, sink

        if transport_error or sink is None:
            return
        task = asyncio.create_task(deliver(event))
        try:
            done, _ = await asyncio.wait({task}, timeout=max(0, terminal_deadline - asyncio.get_running_loop().time()))
            if not done:
                transport_error = "terminal event delivery timed out"
            else:
                task.result()
        except Exception as exc:
            transport_error = f"terminal event delivery failed: {type(exc).__name__}: {exc}"
        finally:
            if not task.done():
                sink = None
                await stop_callback(task)
    if result.status == "cancelled":
        for event in build_cancel_terminal_events(handle.session_code):
            await terminal(event)
    elif result.status == "failed":
        errors = [event for event in terminal_events if event["type"] == "RUN_ERROR"]
        await terminal(errors[0] if errors else build_error_event(result.error or "worker failed"))
    else:
        for event in terminal_events:
            await terminal(event)
    value = result.value or {}
    if transport_error:
        return RunOutcome(
            "failed", handle.run_id, handle.session_code,
            error_code="transport.delivery_failed", error_message=transport_error,
            data={"execution_status": result.status, "execution_outcome": value.get("outcome")},
        )
    if kind in ("describe_agent_config", "list_llms", "describe_config_snapshot") and result.status == "finished":
        return RunOutcome("finished", handle.run_id, handle.session_code, data=value)
    if (
        not result.forced
        and result.status in ("finished", "failed")
        and isinstance(value, dict)
        and "outcome" in value
    ):
        data = dict(value["outcome"])
        data["run_id"] = handle.run_id
        if result.status == "failed":
            data["status"] = "failed"
        return RunOutcome(**data)
    return RunOutcome(
        status=result.status,
        run_id=handle.run_id,
        session_code=handle.session_code,
        error_code="agent.internal_error" if result.status == "failed" else "",
        error_message=result.error,
    )


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


@dataclass
class Slot:
    active: Admission | None = None
    queue: deque[Admission] = field(default_factory=deque)


class RunQueue:
    """Per-session FIFO slots and bounded completed results; ``execute`` runs one admitted turn."""

    def __init__(
        self,
        sessions: SessionRegistry,
        execute=None,
        *,
        history_path: str = "",
        busy_mode: str = "reject",
        completed_limit: int = 128,
        on_reap_failure=None,
        hooks: ProcessHooks | None = None,
    ):
        self._sessions = sessions
        self._execute = execute or partial(execute_turn, history_path=history_path, hooks=hooks)
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
        item = Admission(handle, snapshot, sink)
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
                outcome = await self._execute(
                    handle=handle,
                    session=self._sessions.get(handle.session_code),
                    sink=item.sink,
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