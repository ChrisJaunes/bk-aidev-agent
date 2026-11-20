# -*- coding: utf-8 -*-
"""acp-dsh 端到端：真子进程 + JSON-RPC（ACP v1 + _aidev/* 扩展），fake 模型，客户端应答 hook。"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.integration._fake_llm import FakeLLMServer
from tests.integration._paths import AGENT_ROOT, PLUGIN_ROOT, PYTHON

ENTRY = Path(__file__).with_name("_acp_dsh_entry.py")
TIMEOUT = 60.0

HookAnswer = Callable[[dict[str, Any]], dict[str, Any]]


class Client:
    """最小 ACP 客户端：发请求、收通知、自动应答 agent → client 的 _aidev/hook。"""

    def __init__(self, process: subprocess.Popen, hook_answer: HookAnswer | None) -> None:
        self.process = process
        self.hook_answer = hook_answer or (lambda params: {})
        self.hook_requests: list[dict[str, Any]] = []
        self.notifications: list[dict[str, Any]] = []
        self._responses: dict[Any, queue.Queue] = {}
        self._lock = threading.Lock()
        self._seq = 0
        threading.Thread(target=self._pump, daemon=True).start()

    def _send(self, message: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def _send_raw(self, line: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()

    def _pump(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            message = json.loads(line)
            if "method" in message and "id" in message:
                self.hook_requests.append(message)
                assert message["method"] == "_aidev/hook"
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": self.hook_answer(message["params"])})
            elif "method" in message:
                self.notifications.append(message)
            else:
                self._slot(message["id"]).put(message)

    def _slot(self, request_id: Any) -> queue.Queue:
        with self._lock:
            return self._responses.setdefault(request_id, queue.Queue())

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._seq += 1
            request_id = self._seq
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return self._slot(request_id).get(timeout=TIMEOUT)
        except queue.Empty:
            raise AssertionError(f"{method} 在 {TIMEOUT}s 内没有响应") from None

    def updates(self, session_id: str) -> list[dict[str, Any]]:
        return [
            n["params"]["update"]
            for n in self.notifications
            if n["method"] == "session/update" and n["params"]["sessionId"] == session_id
        ]

    def text(self, session_id: str) -> str:
        return "".join(
            u["content"]["text"] for u in self.updates(session_id) if u["sessionUpdate"] == "agent_message_chunk"
        )


@pytest.fixture
def start(tmp_path: Path) -> Iterator[Callable[..., Client]]:
    processes: list[subprocess.Popen] = []

    def _start(hook_answer: HookAnswer | None = None) -> Client:
        env = dict(os.environ)
        env["MESSAGE_HANDLER_TYPE"] = "inmemory"
        env["PYTHONPATH"] = os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), env.get("PYTHONPATH", "")])
        env["AGENT_TUI_TUI_CONFIG_FILE"] = str(tmp_path / "bkai_tui.json")
        env["BKAI_TUI_LOG_PATH"] = str(tmp_path / "worker.log")
        env["ACP_DSH_TOOL_RECORD"] = str(tmp_path / "tool_calls.txt")
        for key in ("AGENT_TUI_CWD", "BKAI_TUI_ACCESS_TOKEN"):
            env.pop(key, None)
        process = subprocess.Popen(
            [PYTHON, "-u", str(ENTRY)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            cwd=str(AGENT_ROOT),
            env=env,
        )
        processes.append(process)
        client = Client(process, hook_answer)
        init = client.call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})
        assert init["result"]["_meta"]["aidev"]["protocol"] == "acp-dsh"
        return client

    yield _start

    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def _tool_calls(tmp_path: Path) -> list[str]:
    path = tmp_path / "tool_calls.txt"
    return path.read_text(encoding="utf-8").split() if path.exists() else []


def _new(client: Client, server: FakeLLMServer, code: str = "s1", **meta: Any) -> str:
    aidev = {
        "sessionCode": code,
        "agent": {"agent_code": "agent-hook"},
        "llm": {"base_url": server.base_url, "model": "fake-model"},
    }
    aidev.update(meta)
    reply = client.call("session/new", {"cwd": str(AGENT_ROOT), "mcpServers": [], "_meta": {"aidev": aidev}})
    assert "result" in reply, reply
    assert reply["result"]["sessionId"] == code
    return code


def _prompt(client: Client, session_id: str, text: str) -> dict[str, Any]:
    return client.call("session/prompt", {"sessionId": session_id, "prompt": [{"type": "text", "text": text}]})


@pytest.mark.slow
def test_initialize_advertises_extensions_and_close(start) -> None:
    client = start()
    reply = client.call("initialize", {"protocolVersion": 1})
    result = reply["result"]
    assert result["protocolVersion"] == 1
    assert result["agentCapabilities"]["sessionCapabilities"]["close"] == {}
    assert result["_meta"]["aidev"]["agentMethods"] == ["_aidev/resume"]
    assert "_aidev/hook" in result["_meta"]["aidev"]["clientMethods"]


@pytest.mark.slow
def test_plain_turn_streams_text_and_finishes(start) -> None:
    with FakeLLMServer(["你好呀"]) as llm:
        client = start()
        sid = _new(client, llm)
        reply = _prompt(client, sid, "hi")
    assert reply["result"]["stopReason"] == "end_turn"
    assert reply["result"]["_meta"]["aidev"]["status"] == "finished"
    assert client.text(sid) == "你好呀"
    assert client.hook_requests == []  # 未下发 hooks → 完全关闭


@pytest.mark.slow
def test_hooks_round_trip_and_deny_blocks_tool(start, tmp_path: Path) -> None:
    def answer(params: dict[str, Any]) -> dict[str, Any]:
        if params["point"] == "tool_pre":
            return {"decision": "deny", "reason": "不许"}
        if params["point"] == "session_start":
            return {"context": ""}
        return {"decision": "accept"}

    with FakeLLMServer(["好的"], tool_calls=[[{"name": "fake_tool", "args": {"q": "x"}, "id": "call_1"}]]) as llm:
        client = start(answer)
        sid = _new(client, llm, hooks={"timeout_ms": 5000})
        reply = _prompt(client, sid, "用工具")

    assert reply["result"]["_meta"]["aidev"]["status"] == "finished"
    points = [r["params"]["point"] for r in client.hook_requests]
    assert points[:2] == ["session_start", "tool_pre"]
    pre = client.hook_requests[1]["params"]
    assert pre["sessionId"] == sid and pre["payload"]["tool_name"] == "fake_tool"
    assert _tool_calls(tmp_path) == []
    # deny 的 reason 作为工具结果回到模型（工具本体零执行）
    assert "不许" in json.dumps(llm.requests[-1], ensure_ascii=False)
    kinds = [u["sessionUpdate"] for u in client.updates(sid)]
    assert kinds[0] == "tool_call" and kinds[-1] == "agent_message_chunk"


@pytest.mark.slow
def test_ask_interrupts_then_resume_runs_tool(start, tmp_path: Path) -> None:
    def answer(params: dict[str, Any]) -> dict[str, Any]:
        if params["point"] == "tool_pre":
            return {"decision": "ask", "reason": "确认一下"}
        return {}

    with FakeLLMServer(["完成"], tool_calls=[[{"name": "fake_tool", "args": {"q": "y"}, "id": "call_9"}]]) as llm:
        client = start(answer)
        sid = _new(client, llm, hooks={"timeout_ms": 5000, "points": ["tool_pre"]})
        first = _prompt(client, sid, "用工具")
        meta = first["result"]["_meta"]["aidev"]
        assert meta["status"] == "interrupt"
        interrupt = meta["interrupts"][0]
        assert interrupt["reason"] == "aidev:hook_ask"
        assert _tool_calls(tmp_path) == []

        resumed = client.call(
            "_aidev/resume",
            {"sessionId": sid, "resume": {"interruptId": interrupt["id"], "decision": "allow"}},
        )
    assert resumed["result"]["_meta"]["aidev"]["status"] == "finished", resumed
    assert _tool_calls(tmp_path) == ["y"]


@pytest.mark.slow
def test_errors_carry_stable_codes_and_close_is_idempotent(start) -> None:
    with FakeLLMServer(["x"]) as llm:
        client = start()
        missing = _prompt(client, "nope", "hi")
        assert missing["error"]["data"]["code"] == "session.not_found"

        bad = client.call("session/new", {"cwd": str(AGENT_ROOT), "mcpServers": [], "_meta": {"aidev": {}}})
        assert bad["error"]["code"] == -32602
        assert bad["error"]["data"]["code"] == "protocol.invalid_params"

        sid = _new(client, llm)
        assert "result" in client.call("session/close", {"sessionId": sid})
        assert "result" in client.call("session/close", {"sessionId": sid})
        assert _prompt(client, sid, "hi")["error"]["data"]["code"] == "session.not_found"


@pytest.mark.slow
def test_model_failure_is_a_json_rpc_error(start) -> None:
    with FakeLLMServer(error_status=500) as llm:
        client = start()
        sid = _new(client, llm)
        reply = _prompt(client, sid, "hi")
    assert reply["error"]["data"]["code"] == "agent.internal_error"


@pytest.mark.slow
def test_bad_requests_keep_the_connection_usable_and_never_leak_credentials(start) -> None:
    """坏请求逐条得到稳定错误码，连接保持可用；凭据不出现在任何协议帧里。"""
    secret = "SECRET-TOKEN-DO-NOT-LEAK"
    with FakeLLMServer(["还活着"]) as llm:
        client = start()
        client._send_raw("{broken json")
        unknown = client.call("_aidev/teleport", {})
        assert unknown["error"]["code"] == -32601

        assert _prompt(client, "ghost", "hi")["error"]["data"]["code"] == "session.not_found"

        sid = _new(
            client,
            llm,
            "s1",
            llm={"base_url": llm.base_url, "model": "fake-model", "auth_headers": {"X-Bkapi-Authorization": secret}},
        )
        client.call("session/close", {"sessionId": sid})
        assert _prompt(client, sid, "hi")["error"]["data"]["code"] == "session.not_found"

        bad_build = client.call(
            "session/new",
            {
                "cwd": str(AGENT_ROOT),
                "mcpServers": [],
                "_meta": {"aidev": {"sessionCode": "s2", "agent": {"agent_code": "x"}, "llm": "nope"}},
            },
        )
        assert bad_build["error"]["data"]["code"] == "protocol.invalid_params"

        alive = _new(client, llm, "s3")
        reply = _prompt(client, alive, "还活着吗")
    assert reply["result"]["_meta"]["aidev"]["status"] == "finished"
    assert secret not in json.dumps(client.notifications + client.hook_requests, ensure_ascii=False)
