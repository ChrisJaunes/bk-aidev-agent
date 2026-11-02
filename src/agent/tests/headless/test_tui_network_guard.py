"""Network guard probes run in subprocesses; never restrict the pytest process."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

from aidev_agent.headless.network_guard import NetworkBroker, allowed_endpoints

ROOT = Path(__file__).resolve().parents[2]


def guarded(source, broker=None):
    env = {**os.environ, "PYTHONPATH": str(ROOT), "AGENT_TUI_EGRESS_GUARD": "1"}
    descriptors = ()
    if broker:
        env["AGENT_TUI_EGRESS_FD"] = str(broker.fd)
        descriptors = (broker.fd,)
    script = "from aidev_agent.headless.network_guard import install_from_env; install_from_env();\n" + source
    return subprocess.run([sys.executable, "-c", script], env=env, pass_fds=descriptors,
                          capture_output=True, text=True, timeout=30)


def test_allowlist_uses_exact_configured_endpoints():
    result = allowed_endpoints({"LLM_GATEWAY_URL": "https://model.example.invalid/api",
                                "BK_API_URL_TMPL": "http://{api_name}.example.invalid/prod/",
                                "AIDEV_GATEWAY_NAME": "platform"})
    assert result == {("model.example.invalid", 443), ("platform.example.invalid", 80)}
    extended = allowed_endpoints({"LLM_GATEWAY_URL": "https://model.example.invalid"},
                                ["http://child.example.invalid", "http://test", "https://github.com"])
    assert extended == {("model.example.invalid", 443), ("child.example.invalid", 80)}
    with pytest.raises(ValueError):
        allowed_endpoints({"LLM_GATEWAY_URL": "http://127.0.0.1"})
    with pytest.raises(ValueError):
        allowed_endpoints({"LLM_GATEWAY_URL": "https://user:secret@example.invalid"})


def test_native_network_and_other_unix_connections_are_denied():
    result = guarded('''import ctypes, errno, socket, subprocess
lib = ctypes.CDLL(None, use_errno=True)
for family in (socket.AF_INET, socket.AF_INET6, socket.AF_PACKET, socket.AF_NETLINK):
    assert lib.socket(family, socket.SOCK_STREAM, 0) == -1
    assert ctypes.get_errno() == errno.EPERM
for kind in (socket.SOCK_DGRAM, socket.SOCK_RAW, socket.SOCK_SEQPACKET):
    assert lib.socket(socket.AF_UNIX, kind, 0) == -1
    assert ctypes.get_errno() == errno.EPERM
s = socket.socket(socket.AF_UNIX)
try: s.connect('/tmp/should-not-connect.sock')
except PermissionError: pass
else: raise AssertionError('Unix connect bypass')
a,b = socket.socketpair()
a.sendall(b'works'); assert b.recv(5)==b'works'; a.close(); b.close()
p = subprocess.run(['curl','--noproxy','*','-m','3','https://github.com'],capture_output=True)
assert p.returncode != 0
print('denied')
''')
    assert result.returncode == 0, result.stderr
    assert "denied" in result.stdout


def test_broker_denies_public_domain_even_with_python_bridge():
    broker = NetworkBroker({("allowed.example.invalid", 443)})
    try:
        result = guarded('''import socket
for host in ('github.com','patch-diff.githubusercontent.com','127.0.0.1','allowed.example.invalid.evil'):
    try: socket.create_connection((host,443),timeout=3)
    except PermissionError: pass
    else: raise AssertionError('broker policy bypass')
print('broker denied')
''', broker)
        assert result.returncode == 0, result.stderr
        assert len([event for event in broker.audit if event["decision"] == "denied"]) == 4
    finally:
        broker.close()


def test_httpx_and_requests_preserve_transport_through_allowed_broker():
    # Local fixture only: no external services. The broker's fixed destination
    # is authorized explicitly in this test, not added to production policy.
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "https://github.com/sympy/sympy")
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"fixture")
        def log_message(self, *args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    broker = NetworkBroker({("localhost", port)})
    try:
        result = guarded(f'''import httpx,requests
assert requests.get('http://localhost:{port}',timeout=5).text=='fixture'
with httpx.Client(trust_env=False) as client:
    assert client.get('http://localhost:{port}',timeout=5).text=='fixture'
try: requests.get('http://localhost:{port}/redirect',timeout=5)
except requests.exceptions.ConnectionError: pass
else: raise AssertionError('redirect escaped broker policy')
print('clients work')
''', broker)
        assert result.returncode == 0, result.stderr
    finally:
        broker.close()
        server.shutdown()
        server.server_close()
