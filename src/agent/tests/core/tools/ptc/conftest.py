"""Real subprocess bridge fixtures; production never falls back to local exec."""

import asyncio
import base64
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import uuid
from importlib.resources import files
from pathlib import Path

import pytest

from aidev_agent.core.tools.ptc.protocol import PtcError, encode
from aidev_agent.core.tools.ptc.settings import PtcSettings


class LocalBridge:
    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="ptct-", dir="/tmp"))
        self.script = str(files("aidev_agent.core.tools.ptc.sandbox_assets").joinpath("runner.py"))
        self.requests = []
        self.events = []
        self.drop_operation = None
        self.dropped = False

    def run(self, request):
        self.requests.append(request.copy())
        root = self.root / request["execution_id"]
        data = encode(request)
        if len(data) > 48 * 1024:
            payload_file = self.root / f"payload-{uuid.uuid4().hex}.json"
            payload_file.write_bytes(data)
            command = [sys.executable, self.script, "exchange-file", str(root), str(payload_file)]
        else:
            payload = base64.b64encode(data).decode("ascii")
            command = [sys.executable, self.script, "exchange", str(root), payload]
        result = subprocess.run(
            command,
            capture_output=True,
            timeout=10,
            check=True,
        )
        event = json.loads(result.stdout)
        self.events.append(event)
        if request["op"] == self.drop_operation and not self.dropped:
            self.dropped = True
            raise PtcError("TRANSPORT_UNCERTAIN", "Injected lost HTTP response")
        return event

    async def exchange(self, request):
        return await asyncio.to_thread(self.run, request)

    def close(self):
        identities = {event.get("execution_id"): event for event in self.events if "supervisor_pid" in event}
        for execution_id, event in identities.items():
            with contextlib.suppress(Exception):
                self.run({"protocol_version": 1, "execution_id": execution_id, "op": "cancel"})
            with contextlib.suppress(ProcessLookupError):
                os.kill(event["supervisor_pid"], signal.SIGTERM)
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def bridge():
    value = LocalBridge()
    try:
        yield value
    finally:
        value.close()


@pytest.fixture
def ptc_settings():
    return PtcSettings(enabled=True, snapshot="test-image", tool_codecs={"echo": "json"}, wait_seconds=1)


@pytest.fixture
def request_factory():
    execution_id = uuid.uuid4().hex

    def make(operation="start", code="result = 1", **fields):
        request = {"protocol_version": 1, "execution_id": execution_id, "op": operation, "wait_seconds": 1}
        if operation == "start":
            request.update(
                code=code, inputs={}, limits={"execution_seconds": 10, "max_tool_calls": 10, "max_log_bytes": 4096}
            )
        return {**request, **fields}

    return make
