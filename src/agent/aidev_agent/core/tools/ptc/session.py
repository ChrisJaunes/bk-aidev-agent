"""Runtime-scoped serial execution and call-result ledger.

This first version intentionally does not resume across Worker replacement or
human approval. A lost process fails closed; it never re-executes user code.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field
from threading import Event, Lock

from .protocol import TERMINAL_EVENTS, PtcError, digest, encode, validate_event
from .settings import PtcSettings


@dataclass
class ExecutionRecord:
    execution_id: str
    fingerprint: str
    calls: dict[str, tuple[str, dict]] = field(default_factory=dict)
    result: dict | None = None
    instance_id: str | None = None
    worker_pid: int | None = None


class PtcSessionManager:
    def __init__(self, transport, settings: PtcSettings):
        self.transport = transport
        self.settings = settings
        self._records: dict[str, ExecutionRecord] = {}
        self._gate = Lock()
        self._closed = Event()
        self._cached_bytes = 0

    def close(self):
        self._closed.set()
        self.transport.close()

    def _charge_result(self, value):
        size = len(encode(value))
        if self._cached_bytes + size > self.settings.max_cached_bytes:
            raise PtcError("RESULT_BUDGET_EXCEEDED", "PTC Runtime result cache budget exceeded")
        self._cached_bytes += size

    async def _exchange(self, request, deadline):
        for attempt in range(self.settings.reconnect_attempts + 1):
            if self._closed.is_set():
                raise PtcError("RUNTIME_CLOSED", "PTC Runtime has ended")
            if time.monotonic() >= deadline:
                raise PtcError("EXECUTION_TIMEOUT", "PTC execution deadline exceeded")
            try:
                return await self.transport.exchange(request)
            except PtcError as error:
                if error.code != "TRANSPORT_UNCERTAIN" or attempt == self.settings.reconnect_attempts:
                    raise
                await asyncio.sleep(min(0.1 * 2**attempt, 1))

    def _request(self, record, operation, **fields):
        return {
            "protocol_version": 1,
            "execution_id": record.execution_id,
            "op": operation,
            "op_id": uuid.uuid4().hex,
            "wait_seconds": self.settings.wait_seconds,
            **fields,
        }

    async def execute(self, key: str, code: str, inputs: dict, invoke_tool):
        if not self._gate.acquire(blocking=False):
            raise PtcError("EXECUTION_BUSY", "Only one PTC execution may run in this Runtime")
        record = None
        try:
            if self._closed.is_set():
                raise PtcError("RUNTIME_CLOSED", "PTC Runtime has ended")
            if len(code.encode("utf-8")) > self.settings.max_code_bytes:
                raise PtcError("CODE_TOO_LARGE", "PTC code exceeds its byte limit")
            identity = digest({"code": code, "inputs": inputs})
            record = self._records.get(key)
            if record is not None:
                if record.fingerprint != identity:
                    raise PtcError("START_CONFLICT", "This tool call already refers to different PTC code")
                if record.result is None:
                    raise PtcError("EXECUTION_LOST", "Previous PTC execution cannot be safely restarted")
                return record.result
            if len(self._records) >= self.settings.max_executions:
                raise PtcError("EXECUTION_BUDGET_EXCEEDED", "PTC Runtime execution budget exceeded")
            record = self._records[key] = ExecutionRecord(uuid.uuid4().hex, identity)
            return await self._run(record, code, inputs, invoke_tool)
        finally:
            self._gate.release()

    async def _run(self, record, code, inputs, invoke_tool):
        deadline = time.monotonic() + self.settings.execution_seconds
        request = self._request(
            record,
            "start",
            code=code,
            inputs=inputs,
            limits={
                "execution_seconds": self.settings.execution_seconds,
                "max_tool_calls": self.settings.max_tool_calls,
                "max_log_bytes": self.settings.max_log_bytes,
            },
        )
        acknowledged = 0
        try:
            while True:
                event = validate_event(await self._exchange(request, deadline), record.execution_id, record.instance_id)
                record.instance_id = event["runner_instance_id"]
                if record.worker_pid is not None and event["worker_pid"] != record.worker_pid:
                    raise PtcError("EXECUTION_LOST", "PTC code process changed")
                record.worker_pid = event["worker_pid"]
                kind = event["type"]
                if kind in TERMINAL_EVENTS:
                    if event["seq"] != acknowledged + 1:
                        raise PtcError("PROTOCOL_ERROR", "Invalid PTC terminal sequence")
                    if kind == "finished":
                        encode(event.get("result"), self.settings.max_result_bytes)
                    encode(event, self.settings.max_result_bytes + 32 * 1024)
                    self._charge_result(event)
                    record.result = event
                    return event
                if kind == "running":
                    if event["seq"] != acknowledged:
                        raise PtcError("PROTOCOL_ERROR", "Invalid PTC wait sequence")
                    request = self._request(record, "attach", after_seq=acknowledged)
                    continue
                if event["seq"] != acknowledged + 1:
                    raise PtcError("PROTOCOL_ERROR", "PTC event sequence skipped or moved backwards")
                call_id = event["call_id"]
                identity = digest({"name": event["name"], "arguments": event["arguments"]})
                cached = record.calls.get(call_id)
                if cached is not None:
                    if cached[0] != identity:
                        raise PtcError("PROTOCOL_ERROR", "PTC call changed while replaying")
                    response = cached[1]
                else:
                    if len(record.calls) >= self.settings.max_tool_calls:
                        raise PtcError("TOOL_BUDGET_EXCEEDED", "PTC tool budget exceeded")
                    seconds = min(self.settings.tool_seconds, deadline - time.monotonic())
                    if seconds <= 0:
                        raise PtcError("EXECUTION_TIMEOUT", "PTC execution deadline exceeded")
                    try:
                        response = await asyncio.wait_for(invoke_tool(event), timeout=seconds)
                    except TimeoutError:
                        raise PtcError("TOOL_TIMEOUT", "PTC tool timed out; outcome may be uncertain") from None
                    encode(response, self.settings.max_result_bytes)
                    self._charge_result(response)
                    record.calls[call_id] = (identity, response)
                acknowledged = event["seq"]
                request = self._request(record, "resume", call_id=call_id, result=response, after_seq=acknowledged)
        except BaseException as error:
            code = error.code if isinstance(error, PtcError) else "EXECUTION_ABORTED"
            # Store the failure before cleanup: graph replay cannot restart code.
            record.result = {
                "type": "failed",
                "execution_id": record.execution_id,
                "error": {"code": code, "message": code},
            }
            # Dedicated sandbox TTL and Runtime.close remain the backstop.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self.transport.exchange(self._request(record, "cancel", after_seq=acknowledged)), timeout=5
                )
            raise
