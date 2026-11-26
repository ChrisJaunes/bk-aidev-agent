"""PaaS HTTP transport pinned to one sandbox; never rebuilds on resume."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from importlib.resources import files
from threading import RLock

from requests.exceptions import ConnectionError, HTTPError, Timeout

from .protocol import PtcError, encode
from .settings import PtcSettings


class PaasPtcTransport:
    def __init__(self, backend, settings: PtcSettings):
        self.backend = backend
        self.settings = settings
        self.scope_id = uuid.uuid4().hex
        self.asset = f"/tmp/bkai_ptc_{self.scope_id}.py"
        self.sandbox_id: str | None = None
        self._ready = False
        self._closed = False
        self._lock = RLock()

    def _prepare(self):
        with self._lock:
            if self._closed:
                raise PtcError("RUNTIME_CLOSED", "PTC Runtime has ended")
            if self._ready:
                return
            if self.sandbox_id is None:
                # Dedicated backend: no skill PV state or credential injection.
                self.sandbox_id = self.backend._ensure_sandbox()
            asset = files("aidev_agent.core.tools.ptc.sandbox_assets").joinpath("runner.py").read_bytes()
            self.backend.upload_file(self.sandbox_id, self.asset, asset)
            self._ready = True

    def _exchange(self, request: dict) -> dict:
        try:
            data = encode(request)
            self._prepare()
            root = f"/tmp/ptc-{request['execution_id']}"
            if len(data) > 48 * 1024:
                # Keep argv below Linux MAX_ARG_STRLEN. File transfers are extra
                # HTTP requests and deliberately excluded from the N+1 fast path.
                payload_path = f"/tmp/bkai_ptc_payload_{self.scope_id}_{request['op_id']}.json"
                self.backend.upload_file(self.sandbox_id, payload_path, data)
                command = ["python3", self.asset, "exchange-file", root, payload_path]
            else:
                payload = base64.b64encode(data).decode("ascii")
                command = ["python3", self.asset, "exchange", root, payload]
            result = self.backend.exec_command(
                self.sandbox_id,
                command,
                timeout=self.settings.http_timeout,
            )
            if result.exit_code != 0 or len(result.stdout.encode("utf-8")) > 1024 * 1024 + 1:
                raise PtcError("TRANSPORT_UNCERTAIN", "PTC bridge did not return a complete response")
            try:
                event = json.loads(result.stdout)
            except (ValueError, TypeError) as error:
                raise PtcError("TRANSPORT_UNCERTAIN", "Invalid PTC bridge response") from error
            if isinstance(event, dict) and "transport_error" in event:
                code = event["transport_error"]
                known = {"START_CONFLICT", "RESUME_CONFLICT", "STALE_CALL", "INVALID_CURSOR", "EXECUTION_LOST"}
                raise PtcError(code if code in known else "PROTOCOL_ERROR", "PTC bridge rejected the request")
            return event
        except HTTPError as error:
            if getattr(error.response, "status_code", None) == 404:
                raise PtcError("EXECUTION_LOST", "The original PTC sandbox no longer exists") from error
            raise PtcError("TRANSPORT_UNCERTAIN", "PTC sandbox request failed") from error
        except (Timeout, ConnectionError) as error:
            raise PtcError("TRANSPORT_UNCERTAIN", "PTC sandbox connection was interrupted") from error

    async def exchange(self, request: dict) -> dict:
        return await asyncio.to_thread(self._exchange, request)

    def close(self):
        with self._lock:
            self._closed = True
            self.backend.close()
