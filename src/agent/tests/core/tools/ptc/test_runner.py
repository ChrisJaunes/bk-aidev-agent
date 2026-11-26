"""Cross-request execution stack, event replay, framing and cancellation."""

import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor

import pytest


class TestRunner:
    def test_same_process_and_stack_survive_requests(self, bridge, request_factory):
        code = (
            "import os\nfrom agent_tools import call\n"
            "def nested():\n    values = []\n    identity = id(values)\n"
            "    for i in range(2):\n        values.append(call('echo', {'i': i}))\n"
            "    return [os.getpid(), identity == id(values), values]\nresult = nested()"
        )
        first = bridge.run(request_factory(code=code))
        second = bridge.run(request_factory("resume", call_id=first["call_id"], result={"ok": True, "value": "你好"}))
        last = bridge.run(request_factory("resume", call_id=second["call_id"], result={"ok": True, "value": 2}))
        assert last["result"] == [first["worker_pid"], True, ["你好", 2]]
        assert {e["worker_pid"] for e in (first, second, last)} == {first["worker_pid"]}
        assert first["worker_pid"] != first["supervisor_pid"] != os.getpid()
        assert len(bridge.requests) == 3

    @pytest.mark.parametrize("operation", ["start", "resume"])
    def test_duplicate_operations_replay_without_advancing(self, bridge, request_factory, operation):
        initial = request_factory(code="from agent_tools import call\nresult = [call('echo', {}), call('echo', {})]")
        first = bridge.run(initial)
        resume = request_factory("resume", call_id=first["call_id"], result={"ok": True, "value": 1})
        second = bridge.run(resume)
        replay = bridge.run(initial if operation == "start" else resume)
        expected = first if operation == "start" else second
        assert replay == expected
        last = bridge.run(request_factory("resume", call_id=second["call_id"], result={"ok": True, "value": 2}))
        assert last["result"] == [1, 2]

    def test_conflicting_results_are_rejected(self, bridge, request_factory):
        first = bridge.run(request_factory(code="from agent_tools import call\nresult = call('echo', {})"))
        request = request_factory("resume", call_id=first["call_id"], result={"ok": True, "value": 1})
        bridge.run(request)
        assert bridge.run({**request, "result": {"ok": True, "value": 2}}) == {"transport_error": "RESUME_CONFLICT"}

    def test_long_compute_can_attach_and_cancel(self, bridge, request_factory):
        request = request_factory(code="import time\ntime.sleep(30)", wait_seconds=0.02)
        first = bridge.run(request)
        assert first["type"] == "running"
        attached = bridge.run(request_factory("attach", wait_seconds=0.02))
        assert attached["worker_pid"] == first["worker_pid"]
        stopped = bridge.run(request_factory("cancel"))
        assert stopped["type"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            os.kill(first["worker_pid"], 0)

    def test_worker_does_not_inherit_credentials(self, bridge, request_factory, monkeypatch):
        monkeypatch.setenv("ACCESS_TOKEN", "must-stay-in-runtime")
        response = bridge.run(request_factory(code="import os\nresult = os.environ.get('ACCESS_TOKEN')"))
        assert response["type"] == "finished"
        assert response["result"] is None

    def test_worker_crash_is_terminal_not_restarted(self, bridge, request_factory):
        request = request_factory(code="import os\nos.kill(os.getpid(), 9)")
        event = bridge.run(request)
        assert event["type"] == "failed"
        assert bridge.run(request) == event

    def test_log_flood_is_drained_and_bounded(self, bridge, request_factory):
        request = request_factory(code="print('x' * 200000)\nresult = 42")
        event = bridge.run(request)
        assert event["type"] == "finished"
        assert event["result"] == 42
        assert len(event["stdout"]) <= 4096
        assert event["stdout_truncated"]

    def test_concurrent_start_only_creates_one_code_process(self, bridge, request_factory):
        request = request_factory(code="from agent_tools import call\nresult = call('echo', {})")
        with ThreadPoolExecutor(max_workers=2) as pool:
            events = list(pool.map(bridge.run, [request, request]))
        assert events[0] == events[1]

    def test_client_disconnect_preserves_the_next_event(self, bridge, request_factory):
        first = bridge.run(
            request_factory(
                code="import time\nfrom agent_tools import call\ntime.sleep(.2)\nresult = call('echo', {})",
                wait_seconds=0,
            )
        )
        request = request_factory("attach", wait_seconds=1)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.connect(str(bridge.root / first["execution_id"] / "s"))
            client.sendall((json.dumps(request) + "\n").encode())
        event = bridge.run(request)
        assert event["type"] == "tool_call" and event["seq"] == 1
        assert event["worker_pid"] == first["worker_pid"]

    def test_execution_deadline_kills_the_code_process(self, bridge, request_factory):
        request = request_factory(code="import time\ntime.sleep(30)")
        request["limits"]["execution_seconds"] = 0.1
        event = bridge.run(request)
        assert event["type"] == "failed"
        assert event["error"]["code"] == "EXECUTION_TIMEOUT"

    def test_file_payload_path_matches_inline_execution(self, bridge, request_factory):
        import subprocess
        import sys

        request = request_factory(code="result = '你好'")
        payload = bridge.root / "payload.json"
        payload.write_text(json.dumps(request))
        root = bridge.root / request["execution_id"]
        result = subprocess.run(
            [sys.executable, bridge.script, "exchange-file", str(root), str(payload)],
            capture_output=True,
            check=True,
            timeout=10,
        )
        event = json.loads(result.stdout)
        bridge.events.append(event)
        assert event["result"] == "你好" and not payload.exists()


def test_cancel_is_responsive_when_code_stops_reading_results(bridge, request_factory):
    import time

    code = (
        "from agent_tools import call\nimport socket, json, time\n"
        "channel = next(c.cell_contents for c in call.__closure__ if isinstance(c.cell_contents, socket.socket))\n"
        "channel.sendall((json.dumps({'kind': 'call', 'name': 'echo', 'arguments': {}}) + '\\n').encode())\n"
        "time.sleep(30)"
    )
    first = bridge.run(request_factory(code=code))
    resume = request_factory("resume", call_id=first["call_id"], result={"ok": True, "value": "x" * 600000})
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(bridge.run, resume)
        time.sleep(0.1)
        stopped = bridge.run(request_factory("cancel"))
        assert stopped["type"] == "cancelled"
        assert waiting.result(timeout=5)["type"] == "cancelled"
