# -*- coding: utf-8 -*-
"""Production SDK runner: typed snapshots, spawn IPC, joined-process finalization."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from typing import Any

from .contracts import EventConsumer, RunHandle, RunOutcome
from .history import LocalHistory
from .models import HookHandler, HookReply, HookRequest, TurnSnapshot, event_payload
from .run import Run
from .session import Session
from .terminal import build_cancel_terminal_events, build_error_event
from .worker import ProcessWorker, WorkerSpec, stop_callback

#: 跨 spawn 边界传递的**唯一**工厂地址：子进程 ``_worker_entry`` 在完成
#: ``install_worker_startup`` 之后才 ``import_module`` 到这里，执行体顶部的 SDK 依赖因此
#: 落在 guard / 环境 / OTel 之后就位（见 ``runtime.sdk`` 包 docstring）。
WORKER_FACTORY = "aidev_agent_cli.runtime.sdk.executor:build"


async def _call_sink(sink: EventConsumer, event: dict[str, Any]) -> None:
    """Deliver one typed event to the run's asynchronous destination."""
    send = getattr(sink, "send", None)
    await (send(event) if send is not None else sink(event))


TERMINAL_DELIVERY_SECONDS = 1.0


class SDKRunner:
    """One turn owns one ProcessWorker; no SDK objects cross the spawn boundary."""

    async def execute(
        self,
        *,
        handle: RunHandle,
        session: Session,
        sink: EventConsumer | None,
        hook_handler: HookHandler | None,
        snapshot: TurnSnapshot,
    ) -> RunOutcome:
        kind = snapshot.kind
        session.ensure_open()
        if handle.token.cancelled:
            return RunOutcome(status="cancelled", run_id=handle.run_id, session_code=handle.session_code)
        snapshot = replace(snapshot, run_id=handle.run_id, correlation_id=snapshot.correlation_id or handle.run_id)
        run = Run(
            handle,
            snapshot,
            ProcessWorker(WorkerSpec(WORKER_FACTORY, snapshot.payload())),
            sink,
            hook_handler,
        )
        worker = run.worker

        build_started = asyncio.get_running_loop().time()
        build_timed_out = False
        terminal_events = []
        finalizing = False

        async def deliver(event: Any) -> None:
            if isinstance(event, dict) and event.get("type") in ("RUN_ERROR", "RUN_FINISHED") and not finalizing:
                terminal_events.append(event)
                return
            target = run.sink
            if target is not None:
                await _call_sink(target, event)

        def phase_changed(phase: str) -> None:
            nonlocal build_started
            build_started = asyncio.get_running_loop().time() if phase == "building" else None
            handle.mark_phase(phase)

        async def hook(name: str, payload: Any) -> Any:
            if name != "hook" or run.hook_handler is None:
                return asdict(HookReply("timeout"))
            return asdict(await run.hook_handler(HookRequest(**payload), handle.token))

        async def watch_cancel() -> None:
            nonlocal build_timed_out
            while True:
                if build_started is not None and asyncio.get_running_loop().time() - build_started >= 180:
                    build_timed_out = True
                    worker.cancel()
                if handle.token.cancelled:
                    if handle.token.cancel_grace is not None:
                        worker.cancel_grace = min(worker.cancel_grace, handle.token.cancel_grace)
                    worker.cancel()
                await asyncio.sleep(0.005)

        watcher = asyncio.create_task(watch_cancel())
        try:
            result = await worker.run(event_sink=deliver, request_handler=hook, phase_handler=phase_changed)
        finally:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
        if build_timed_out:
            result = replace(result, status="failed", error="agent build timed out")

        def record_final_facts():
            history = LocalHistory()
            try:
                persisted_status = result.status
                if result.status == "finished" and isinstance(result.value, dict):
                    persisted_status = result.value.get("outcome", {}).get("status", persisted_status)
                history.finalize(
                    handle.run_id,
                    commit=not result.forced and isinstance(result.value, dict) and "outcome" in result.value,
                )
                history.record_run(handle.run_id, handle.session_code, persisted_status, forced=result.forced)
                return history.load(handle.session_code) if kind in ("start", "resume") else None
            finally:
                history.close()

        state = await asyncio.to_thread(record_final_facts)
        finalizing = True
        transport_error = ""
        terminal_deadline = asyncio.get_running_loop().time() + TERMINAL_DELIVERY_SECONDS

        async def terminal(event):
            nonlocal transport_error

            if transport_error or run.sink is None:
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
                    run.sink = None
                    await stop_callback(task)
        if result.status == "cancelled":
            for event in build_cancel_terminal_events(handle.session_code):
                await terminal(event_payload(event))
        elif result.status == "failed":
            errors = [event for event in terminal_events if event["type"] == "RUN_ERROR"]
            await terminal(errors[0] if errors else event_payload(build_error_event(result.error or "worker failed")))
        else:
            for event in terminal_events:
                await terminal(event)
        value = result.value or {}
        if state is not None:
            session.pending = state.get("interrupts", [])
            session.turn_count = state.get("turn_count", 0)
            session.records = state.get("records", [])
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