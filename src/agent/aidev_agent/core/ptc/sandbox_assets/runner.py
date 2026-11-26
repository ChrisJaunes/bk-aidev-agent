"""Standalone Unix-socket PTC runner. No SDK or network service dependency.

Only JSON crosses the untrusted code boundary. Supervisor and code have separate
process groups; clients may disconnect without consuming an event.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import types
import uuid
from pathlib import Path

MAX_FRAME = 1024 * 1024


def pack(value):
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("PAYLOAD_TOO_LARGE")
    return data + b"\n"


def receive(reader):
    line = reader.readline(MAX_FRAME + 2)
    if not line or not line.endswith(b"\n") or len(line) > MAX_FRAME + 1:
        raise ValueError("INVALID_FRAME")
    return json.loads(line, parse_constant=lambda value: invalid_constant(value))


def invalid_constant(value):
    raise ValueError(f"Invalid JSON constant: {value}")


def fingerprint(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def terminate(process):
    if process is None:
        return
    # Kill the group even if its leader has already exited: children may remain.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=3)


class Execution:
    def __init__(self, root):
        self.root = root
        self.instance_id = uuid.uuid4().hex
        self.condition = threading.Condition(threading.RLock())
        self.events = []
        self.accepted = {}
        self.pending = None
        self.process = None
        self.channel = None
        self.request_hash = None
        self.execution_id = None
        self.deadline = None
        self.finished_at = None
        self.logs = bytearray()
        self.log_truncated = False
        self.call_count = 0

    def base(self):
        return {
            "protocol_version": 1,
            "execution_id": self.execution_id,
            "runner_instance_id": self.instance_id,
            "supervisor_pid": os.getpid(),
            "worker_pid": self.process.pid if self.process is not None else None,
        }

    def emit(self, kind, **fields):
        event = {**self.base(), "seq": len(self.events) + 1, "type": kind, **fields}
        pack(event)
        self.events.append(event)
        if kind in {"finished", "failed", "cancelled"}:
            self.finished_at = time.monotonic()
            self.pending = None
        self.condition.notify_all()

    def fail(self, code):
        with self.condition:
            if self.finished_at is None:
                self.emit("failed", error={"code": code, "message": code})
        terminate(self.process)

    def start(self, request):
        payload = {key: request[key] for key in ("execution_id", "code", "inputs", "limits")}
        request_hash = fingerprint(payload)
        if self.request_hash is not None:
            if request_hash != self.request_hash:
                raise ValueError("START_CONFLICT")
            return
        self.request_hash = request_hash
        self.execution_id = request["execution_id"]
        self.limits = request["limits"]
        self.deadline = time.monotonic() + self.limits["execution_seconds"]
        (self.root / "input.json").write_bytes(pack(payload))
        parent, child = socket.socketpair()
        try:
            self.process = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "worker", str(self.root), str(child.fileno())],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1"},
                cwd=self.root,
                pass_fds=(child.fileno(),),
                start_new_session=True,
            )
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
        self.channel = parent
        self.log_thread = threading.Thread(target=self.drain_logs, daemon=True)
        self.log_thread.start()
        threading.Thread(target=self.read_worker, daemon=True).start()

    def drain_logs(self):
        with self.process.stdout as stream:
            while chunk := stream.read(4096):
                with self.condition:
                    remaining = self.limits["max_log_bytes"] - len(self.logs)
                    self.logs.extend(chunk[: max(remaining, 0)])
                    self.log_truncated |= len(chunk) > remaining

    def read_worker(self):
        try:
            with self.channel.makefile("rb") as reader:
                while True:
                    message = receive(reader)
                    with self.condition:
                        if self.finished_at is not None:
                            return
                        kind = message.get("kind")
                        if kind == "call":
                            self.call_count += 1
                            if self.pending is not None or self.call_count > self.limits["max_tool_calls"]:
                                raise ValueError("TOOL_BUDGET_EXCEEDED")
                            seq = len(self.events) + 1
                            self.pending = f"{self.execution_id}:{seq}"
                            self.emit(
                                "tool_call", call_id=self.pending, name=message["name"], arguments=message["arguments"]
                            )
                        elif kind in {"finished", "failed"}:
                            break
                        else:
                            raise ValueError("INVALID_WORKER_EVENT")
            self.process.wait(timeout=3)
            # Background children must not keep stdout open after user code ends.
            terminate(self.process)
            self.log_thread.join(timeout=3)
            with self.condition:
                if self.finished_at is None:
                    self.emit(
                        kind,
                        **{key: value for key, value in message.items() if key != "kind"},
                        stdout=self.logs[:16384].decode("utf-8", "replace"),
                        stdout_truncated=self.log_truncated or len(self.logs) > 16384,
                    )
        except BaseException as error:
            self.fail(str(error) if isinstance(error, ValueError) else "WORKER_EXITED")

    def exchange(self, request):
        outgoing = None
        with self.condition:
            operation = request["op"]
            if operation == "start":
                self.start(request)
                cursor = 0
            else:
                if request["execution_id"] != self.execution_id:
                    raise ValueError("EXECUTION_LOST")
                cursor = request.get("after_seq", 0)
            if operation == "resume":
                call_id, result = request["call_id"], request["result"]
                result_hash = fingerprint(result)
                previous = self.accepted.get(call_id)
                if previous is not None:
                    if previous[0] != result_hash:
                        raise ValueError("RESUME_CONFLICT")
                    cursor = previous[1]
                else:
                    if call_id != self.pending or self.finished_at is not None:
                        raise ValueError("STALE_CALL")
                    cursor = self.events[-1]["seq"]
                    self.accepted[call_id] = (result_hash, cursor)
                    self.pending = None
                    # Commit acceptance before sending: uncertain delivery fails
                    # the execution instead of injecting a possibly duplicate input.
                    outgoing = result
            elif operation == "cancel":
                if self.finished_at is None:
                    self.emit("cancelled", error={"code": "CANCELLED", "message": "Execution cancelled"})
                    terminate(self.process)
                return self.events[-1]
            elif operation not in {"start", "attach"}:
                raise ValueError("INVALID_OPERATION")
            if not isinstance(cursor, int) or isinstance(cursor, bool) or not 0 <= cursor <= len(self.events):
                raise ValueError("INVALID_CURSOR")
        # Never hold the state lock during worker I/O. A code process that
        # stops reading must not prevent cancellation or its deadline.
        if outgoing is not None:
            try:
                self.channel.sendall(pack(outgoing))
            except OSError:
                self.fail("WORKER_DISCONNECTED")
        with self.condition:
            wait_until = time.monotonic() + min(max(float(request.get("wait_seconds", 20)), 0), 30)
            while True:
                if cursor < len(self.events):
                    return self.events[cursor]
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    return {**self.base(), "seq": cursor, "type": "running"}
                self.condition.wait(remaining)


class RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(35)
        try:
            request = receive(self.rfile)
            if request.get("protocol_version") != 1:
                raise ValueError("INVALID_VERSION")
            response = self.server.execution.exchange(request)
        except (ValueError, KeyError, TypeError, OSError) as error:
            response = {"transport_error": str(error)}
        # Events remain buffered when the client disconnects.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError, socket.timeout):
            self.wfile.write(pack(response))


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    block_on_close = False


def supervise(root):
    # The short-lived session leader exits; no captured shell stream is inherited.
    if os.fork() > 0:
        os._exit(0)
    socket_path = root / "s"
    execution = Execution(root)
    with Server(str(socket_path), RequestHandler) as server:
        os.chmod(socket_path, 0o600)
        server.execution = execution

        def watchdog():
            born = time.monotonic()
            while True:
                time.sleep(0.1)
                now = time.monotonic()
                if execution.deadline is not None and execution.finished_at is None and now >= execution.deadline:
                    execution.fail("EXECUTION_TIMEOUT")
                if execution.finished_at is not None and now - execution.finished_at >= 300:
                    break
                if execution.request_hash is None and now - born >= 30:
                    break
            server.shutdown()

        threading.Thread(target=watchdog, daemon=True).start()
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            terminate(execution.process)
            socket_path.unlink(missing_ok=True)


def exchange(root, request):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    socket_path = root / "s"
    if len(os.fsencode(socket_path)) >= 104:
        raise ValueError("SOCKET_PATH_TOO_LONG")
    if request["op"] == "start":
        with (root / "launch.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            marker = root / "launched"
            if not marker.exists():
                marker.touch(mode=0o600, exist_ok=False)
                with (root / "supervisor.log").open("ab", buffering=0) as log:
                    bootstrap = subprocess.Popen(
                        [sys.executable, str(Path(__file__).resolve()), "supervisor", str(root)],
                        stdin=subprocess.DEVNULL,
                        stdout=log,
                        stderr=log,
                        close_fds=True,
                        start_new_session=True,
                        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8"},
                    )
                    if bootstrap.wait(timeout=5) != 0:
                        raise ValueError("BOOTSTRAP_FAILED")
    deadline = time.monotonic() + 5
    while not socket_path.exists():
        if request["op"] != "start" or time.monotonic() >= deadline:
            raise ValueError("EXECUTION_LOST")
        time.sleep(0.01)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(35)
        client.connect(str(socket_path))
        client.sendall(pack(request))
        with client.makefile("rb") as reader:
            return receive(reader)


def worker(root, descriptor):
    request = json.loads((root / "input.json").read_bytes())
    channel = socket.socket(fileno=descriptor)
    reader = channel.makefile("rb")
    module = types.ModuleType("agent_tools")

    class ToolCallError(RuntimeError):
        def __init__(self, code, message):
            self.code = code
            super().__init__(message)

    def call(name, arguments=None):
        if not isinstance(name, str) or not isinstance(arguments or {}, dict):
            raise ValueError("Expected a tool name and an argument object")
        channel.sendall(pack({"kind": "call", "name": name, "arguments": arguments or {}}))
        response = receive(reader)
        if response.get("ok") is not True:
            error = response.get("error") or {}
            raise ToolCallError(error.get("code", "TOOL_FAILED"), error.get("message", "Tool failed"))
        return response.get("value")

    module.call = call
    module.ToolCallError = ToolCallError
    sys.modules["agent_tools"] = module
    namespace = {"__name__": "__ptc__", "inputs": request["inputs"]}
    try:
        exec(compile(request["code"], "<ptc>", "exec"), namespace)
        channel.sendall(pack({"kind": "finished", "result": namespace.get("result")}))
    except BaseException as error:
        with contextlib.suppress(OSError, ValueError):
            channel.sendall(pack({"kind": "failed", "error": {"code": "CODE_ERROR", "message": str(error)[:4096]}}))
    finally:
        reader.close()
        channel.close()


if __name__ == "__main__":
    mode, directory = sys.argv[1:3]
    root = Path(directory)
    if mode == "supervisor":
        supervise(root)
    elif mode == "worker":
        worker(root, int(sys.argv[3]))
    elif mode in {"exchange", "exchange-file"}:
        try:
            if mode == "exchange-file":
                with Path(sys.argv[3]).open("rb") as stream:
                    payload = stream.read(MAX_FRAME + 1)
                Path(sys.argv[3]).unlink(missing_ok=True)
            else:
                payload = base64.b64decode(sys.argv[3], validate=True)
            if len(payload) > MAX_FRAME:
                raise ValueError("PAYLOAD_TOO_LARGE")
            response = exchange(root, json.loads(payload))
        except Exception as error:
            response = {"transport_error": str(error)}
        sys.stdout.buffer.write(pack(response))
    else:
        raise ValueError("INVALID_MODE")
