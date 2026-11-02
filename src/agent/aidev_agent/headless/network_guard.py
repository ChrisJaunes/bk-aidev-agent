"""Opt-in evaluation egress guard. Not enabled in normal TUI sessions."""

from __future__ import annotations

import array
import contextlib
import ctypes
import ctypes.util
import errno
import ipaddress
import json
import os
import select
import socket
import threading
import time
from urllib.parse import urlsplit

_SOCKET = socket.socket
_SOCKETPAIR = socket.socketpair
_GETADDRINFO = socket.getaddrinfo
_INSTALLED = False


def allowed_endpoints(env, platform_urls=()):
    endpoints = set()
    for key in ("LLM_GATEWAY_URL", "LLM_GW_ENDPOINT"):
        if env.get(key):
            parsed = urlsplit(env[key])
            if parsed.scheme not in ("http", "https") or parsed.username or parsed.password or not parsed.hostname:
                raise ValueError(f"invalid evaluation gateway endpoint: {key}")
            endpoints.add((parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)))
    template = env.get("BK_API_URL_TMPL", "")
    gateway = env.get("AIDEV_GATEWAY_NAME", "")
    if template and gateway:
        parsed = urlsplit(template.replace("{api_name}", gateway).replace("{gateway_name}", gateway))
        if parsed.scheme not in ("http", "https") or not parsed.hostname or "{" in parsed.hostname:
            raise ValueError("cannot resolve platform endpoint for egress allowlist")
        endpoints.add((parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)))
    if not endpoints:
        raise ValueError("no model/platform endpoints configured; no unrestricted fallback")
    # Extra URLs must come from the privileged parent's platform deployment
    # snapshot, never from the model or its mutable task directory. Only the
    # configured gateway DNS zone is accepted, not arbitrary public services.
    zones = {host.split('.', 1)[1] for host, _ in endpoints if '.' in host}
    for url in platform_urls:
        parsed = urlsplit(url)
        if (parsed.scheme not in ('http', 'https') or parsed.username or parsed.password
                or not parsed.hostname or not any(parsed.hostname.endswith('.' + zone) for zone in zones)):
            continue
        endpoints.add((parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == 'https' else 80)))
    for host, port in endpoints:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError("evaluation endpoints must be exact hostnames, not IP addresses")
        if port not in (80, 443):
            raise ValueError("evaluation endpoint port must be 80 or 443")
    return endpoints


def _read_line(stream, limit=2048):
    result = bytearray()
    while len(result) < limit:
        value = stream.recv(1)
        if not value:
            raise ConnectionError("broker handshake closed")
        if value == b"\n":
            return bytes(result)
        result.extend(value)
    raise ValueError("broker handshake too long")


class NetworkBroker:
    """Trusted parent-owned TCP broker; no listening network or Unix pathname."""

    def __init__(self, endpoints):
        self.endpoints = set(endpoints)
        self.control, self.child = _SOCKETPAIR()
        self.control.settimeout(0.2)
        self.stopping = threading.Event()
        self.connections = set()
        self.handlers = set()
        self.capacity = threading.BoundedSemaphore(32)
        self.lock = threading.Lock()
        self.audit = []
        self.thread = threading.Thread(target=self._receive, daemon=True)
        self.thread.start()

    @property
    def fd(self):
        return self.child.fileno()

    def _receive(self):
        while not self.stopping.is_set():
            try:
                data, ancillary, flags, _ = self.control.recvmsg(1, socket.CMSG_SPACE(4), socket.MSG_CMSG_CLOEXEC)
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                return
            descriptors = []
            for level, kind, value in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    values = array.array("i")
                    values.frombytes(value[:len(value) - len(value) % values.itemsize])
                    descriptors.extend(values)
            if len(descriptors) != 1 or flags & socket.MSG_CTRUNC:
                for descriptor in descriptors:
                    os.close(descriptor)
                continue
            try:
                peer = _SOCKET(fileno=descriptors[0])
            except OSError:
                os.close(descriptors[0])
                continue
            if not self.capacity.acquire(blocking=False):
                peer.close()
                continue
            handler = threading.Thread(target=self._serve, args=(peer,), daemon=True)
            with self.lock:
                self.connections.add(peer)
                self.handlers.add(handler)
            handler.start()

    def _serve(self, peer):
        upstream = None
        host = None
        port = None
        try:
            peer.settimeout(15)
            request = json.loads(_read_line(peer))
            host = request.get("host")
            port = request.get("port")
            if not isinstance(host, str) or type(port) is not int or (host.lower(), port) not in self.endpoints:
                self.audit.append({"host": host, "port": port, "decision": "denied"})
                peer.sendall(b'{"ok":false,"error":"destination denied"}\n')
                return
            # DNS and TCP exist only here, outside the restricted aidev_agent.headless. The
            # worker still performs end-to-end TLS certificate verification.
            for family, kind, protocol, _, address in _GETADDRINFO(host, port, type=socket.SOCK_STREAM):
                candidate = _SOCKET(family, kind, protocol)
                candidate.settimeout(15)
                try:
                    candidate.connect(address)
                except OSError:
                    candidate.close()
                    continue
                upstream = candidate
                break
            if upstream is None:
                raise OSError("allowed destination is unreachable")
            with self.lock:
                self.connections.add(upstream)
            self.audit.append({"host": host, "port": port, "decision": "allowed"})
            peer.sendall(b'{"ok":true}\n')
            peer.settimeout(20)
            upstream.settimeout(20)
            while not self.stopping.is_set():
                readable, _, _ = select.select([peer, upstream], [], [], 0.2)
                for source in readable:
                    chunk = source.recv(65536)
                    if not chunk:
                        return
                    (upstream if source is peer else peer).sendall(chunk)
        except Exception:
            # Never include request headers, tokens, query strings or response
            # content in a broker diagnostic.
            self.audit.append({"host": host, "port": port, "decision": "connection_error"})
            with contextlib.suppress(OSError):
                peer.sendall(b'{"ok":false,"error":"allowed destination unavailable"}\n')
        finally:
            if upstream:
                upstream.close()
            peer.close()
            with self.lock:
                self.connections.discard(peer)
                self.connections.discard(upstream)
                self.handlers.discard(threading.current_thread())
            self.capacity.release()

    def close(self):
        self.stopping.set()
        self.child.close()
        self.control.close()
        with self.lock:
            for peer in list(self.connections):
                with contextlib.suppress(OSError):
                    peer.shutdown(socket.SHUT_RDWR)
        self.thread.join(timeout=2)
        with self.lock:
            handlers = list(self.handlers)
        deadline = time.monotonic() + 3
        for handler in handlers:
            handler.join(timeout=max(0, deadline - time.monotonic()))


class _ArgCmp(ctypes.Structure):
    _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                ("datum_a", ctypes.c_uint64), ("datum_b", ctypes.c_uint64)]


def install_seccomp():
    library = ctypes.util.find_library("seccomp")
    if not library:
        raise RuntimeError("libseccomp unavailable; refuse unrestricted evaluation")
    seccomp = ctypes.CDLL(library, use_errno=True)
    seccomp.seccomp_init.argtypes = [ctypes.c_uint32]
    seccomp.seccomp_init.restype = ctypes.c_void_p
    seccomp.seccomp_release.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_load.argtypes = [ctypes.c_void_p]
    seccomp.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    seccomp.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    seccomp.seccomp_rule_add_array.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int,
                                             ctypes.c_uint, ctypes.POINTER(_ArgCmp)]
    allow = 0x7FFF0000
    denied = 0x00050000 | errno.EPERM
    context = seccomp.seccomp_init(allow)
    if not context:
        raise RuntimeError("seccomp_init failed")
    try:
        # TSYNC applies to any existing SDK threads; descendants inherit the
        # filter. New architectures are not added: bad-arch defaults to kill.
        if seccomp.seccomp_attr_set(context, 4, 1) != 0:
            raise RuntimeError("seccomp TSYNC unavailable")
        def add(name, comparisons=()):
            syscall = seccomp.seccomp_syscall_resolve_name(name.encode())
            if syscall < 0:
                return
            args = (_ArgCmp * len(comparisons))(*comparisons) if comparisons else None
            if seccomp.seccomp_rule_add_array(context, denied, syscall, len(comparisons), args) != 0:
                raise RuntimeError(f"cannot restrict syscall {name}")
        # AF_UNIX stream pairs are the only communication mechanism. All connect
        # and bind calls are denied, so other local Unix proxies are not usable.
        for name in ("socket", "socketpair"):
            add(name, [_ArgCmp(0, 1, socket.AF_UNIX, 0)])  # SCMP_CMP_NE
            for kind in range(16):
                if kind != socket.SOCK_STREAM:
                    add(name, [_ArgCmp(1, 7, 0xF, kind)])  # SCMP_CMP_MASKED_EQ
        for name in ("connect", "bind", "listen", "io_uring_setup", "bpf", "ptrace",
                     "process_vm_readv", "process_vm_writev", "socketcall"):
            add(name)
        if seccomp.seccomp_load(context) != 0:
            raise RuntimeError("seccomp_load failed; refuse unrestricted evaluation")
    finally:
        seccomp.seccomp_release(context)


def install_bridge(control_fd):
    control = _SOCKET(fileno=os.dup(control_fd))
    os.close(control_fd)
    control.set_inheritable(False)
    lock = threading.Lock()
    class BrokerSocket(_SOCKET):
        def __init__(self, family=socket.AF_INET, type=socket.SOCK_STREAM, proto=0, fileno=None):
            self.broker_peer = None
            self.requested_family = family
            if fileno is None and family in (socket.AF_INET, socket.AF_INET6):
                if type & 0xF != socket.SOCK_STREAM:
                    raise PermissionError("evaluation only supports brokered TCP")
                local, self.broker_peer = _SOCKETPAIR()
                super().__init__(socket.AF_UNIX, socket.SOCK_STREAM, 0, fileno=local.detach())
            else:
                super().__init__(family, type, proto, fileno=fileno)

        def connect(self, address):
            if self.broker_peer is None:
                return super().connect(address)  # blocked by kernel seccomp
            host, port = address[:2]
            timeout = self.gettimeout()
            self.settimeout(20)
            try:
                peer = self.broker_peer
                self.broker_peer = None
                self.sendall((json.dumps({"host": host, "port": int(port)}) + "\n").encode())
                descriptors = array.array("i", [peer.fileno()])
                with lock:
                    control.sendmsg([b"C"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, descriptors)])
                peer.close()
                answer = json.loads(_read_line(self))
                if not answer.get("ok"):
                    raise PermissionError(answer.get("error", "egress denied"))
            finally:
                self.settimeout(timeout)

        def connect_ex(self, address):
            try:
                self.connect(address)
                return 0
            except OSError as exc:
                return exc.errno or errno.EPERM

        def setsockopt(self, level, option, value, *rest):
            if self.requested_family in (socket.AF_INET, socket.AF_INET6) and level in (socket.IPPROTO_TCP, socket.IPPROTO_IP, socket.IPPROTO_IPV6):
                return None
            return super().setsockopt(level, option, value, *rest)

        def close(self):
            if self.broker_peer:
                self.broker_peer.close()
                self.broker_peer = None
            return super().close()

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        if not host:
            raise PermissionError("network bind/DNS lookup is disabled")
        if isinstance(host, bytes):
            host = host.decode("ascii")
        # Preserve the hostname for the broker instead of doing worker-side DNS.
        # Neither spoofing an IP nor monkeypatching this shim bypasses its policy.
        return [(socket.AF_INET, type or socket.SOCK_STREAM, proto or socket.IPPROTO_TCP, "", (host, int(port)))]
    socket.socket = BrokerSocket
    socket.getaddrinfo = getaddrinfo


def install_from_env():
    global _INSTALLED
    if _INSTALLED or os.environ.get("AGENT_TUI_EGRESS_GUARD") != "1":
        return
    descriptor = os.environ.get("AGENT_TUI_EGRESS_FD")
    if descriptor is not None:
        install_bridge(int(descriptor))
    install_seccomp()
    _INSTALLED = True


if __name__ == "__main__":
    import runpy
    import sys
    install_from_env()
    if len(sys.argv) < 2:
        raise SystemExit("expected a script to run under the egress guard")
    script = sys.argv[1]
    sys.argv = sys.argv[1:]
    runpy.run_path(script, run_name="__main__")
