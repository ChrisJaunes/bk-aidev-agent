# -*- coding: utf-8 -*-
"""T-23：无平台依赖与依赖隔离（中断路径，REQ-17 / REQ-19 / ACC-22）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.headless._fake_llm import FakeLLMServer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRET = "HOOK-PATH-AUTH-SECRET"

CHILD_SCRIPT = textwrap.dedent(
    """
    import io, json, sys, threading, time

    from langchain_core.tools import tool

    from aidev_agent.headless import resource_manager as rm_module
    from aidev_agent.headless.cli import Dispatcher
    from aidev_agent.headless.protocol import Request

    calls = {"get_client": 0, "create_tool_approval": 0}
    _original_get_client = rm_module.HeadlessResourceManager.get_client
    _original_ticket = rm_module.HeadlessResourceManager.create_tool_approval

    def _counting_get_client(self, **kwargs):
        calls["get_client"] += 1
        return _original_get_client(self, **kwargs)

    def _counting_ticket(self, *args, **kwargs):
        calls["create_tool_approval"] += 1
        return _original_ticket(self, *args, **kwargs)

    rm_module.HeadlessResourceManager.get_client = _counting_get_client
    rm_module.HeadlessResourceManager.create_tool_approval = _counting_ticket

    tool_calls = []

    @tool
    def probe_tool(q: str) -> str:
        \"\"\"探针工具。\"\"\"
        tool_calls.append(q)
        return f"probe:{q}"

    base_url, model, secret = sys.argv[1], sys.argv[2], sys.argv[3]
    out = io.StringIO()
    dispatcher = Dispatcher(stream=out)

    HOST_RESULTS = {
        "session_start": {"context": "宿主上下文-{}".format(secret[:4])},
        "tool_pre": {"decision": "ask", "reason": "需要人工确认"},
        "tool_post": {"decision": "accept"},
    }

    stop = threading.Event()

    def host_loop():
        handled = set()
        while not stop.is_set():
            for line in out.getvalue().splitlines():
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("type") != "request" or payload.get("id") in handled:
                    continue
                handled.add(payload["id"])
                point = (payload.get("params") or {}).get("point")
                reply = {"id": payload["id"], "ok": True, "result": HOST_RESULTS.get(point, {})}
                dispatcher.handle_line(json.dumps(reply))
            time.sleep(0.005)

    thread = threading.Thread(target=host_loop, daemon=True)
    thread.start()

    dispatcher.handle_request(
        Request(
            id="c1",
            method="create",
            params={
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": base_url, "model": model, "auth_headers": {"X-Bkapi-Authorization": secret}},
                "hooks": {"timeout_ms": 3000},
                "tools": [probe_tool],
            },
        )
    )
    dispatcher.handle_request(Request(id="t1", method="turn", params={"session_code": "s1", "input": "hi"}))
    dispatcher.join(60)

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    turn_response = [line for line in lines if line.get("id") == "t1" and "ok" in line and line.get("type") != "event"]
    interrupts = turn_response[0]["result"].get("interrupts") if turn_response and turn_response[0].get("ok") else None
    interrupt_id = interrupts[0]["id"] if interrupts else None

    if interrupt_id:
        dispatcher.handle_request(
            Request(
                id="r1",
                method="resume",
                params={
                    "session_code": "s1",
                    "resume": {"interruptId": interrupt_id, "decision": "allow"},
                },
            )
        )
        dispatcher.join(60)

    stop.set()
    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    turn_status = [
        line["result"]["status"]
        for line in lines
        if line.get("id") == "t1" and "ok" in line and line.get("type") != "event" and line.get("ok")
    ]
    resume_status = [
        line["result"]["status"]
        for line in lines
        if line.get("id") == "r1" and "ok" in line and line.get("type") != "event" and line.get("ok")
    ]
    summary = {
        "turn_status": turn_status[0] if turn_status else "missing",
        "resume_status": resume_status[0] if resume_status else "missing",
        "tool_calls": len(tool_calls),
        "platform_calls": calls["get_client"],
        "ticket_calls": calls["create_tool_approval"],
        "django_modules": sorted(m for m in sys.modules if m == "django" or m.startswith("django.")),
        "blueapps_modules": sorted(m for m in sys.modules if m == "blueapps" or m.startswith("blueapps.")),
        "protocol_output": out.getvalue(),
    }
    sys.stderr.write("SUMMARY=" + json.dumps(summary) + "\\n")
    """
)


@pytest.fixture
def llm():
    with FakeLLMServer(
        ["恢复完成"],
        tool_calls=[[{"name": "probe_tool", "args": {"q": "x"}, "id": "call_1"}]],
    ) as server:
        yield server


def test_ask_interrupt_resume_has_no_platform_dependency(llm, tmp_path):
    script = tmp_path / "child_hook.py"
    script.write_text(CHILD_SCRIPT, encoding="utf-8")
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    env.pop("RABBITMQ_HOST", None)
    env.pop("REDIS_URL", None)

    result = subprocess.run(
        [sys.executable, str(script), llm.base_url, "fake-model", SECRET],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=str(PROJECT_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr[-3000:]

    summary_line = [line for line in result.stderr.splitlines() if line.startswith("SUMMARY=")]
    assert summary_line, result.stderr[-3000:]
    summary = json.loads(summary_line[-1][len("SUMMARY=") :])

    assert summary["turn_status"] == "interrupt"
    assert summary["resume_status"] == "finished"
    assert summary["tool_calls"] == 1, "放行后工具执行一次"
    assert summary["platform_calls"] == 0, "中断路径不得触达平台客户端"
    assert summary["ticket_calls"] == 0, "中断与恢复不得创建平台单据"
    assert summary["django_modules"] == []
    assert summary["blueapps_modules"] == []

    # 凭据不得出现在 stdout / stderr（hook 请求行只承载点位载荷，不含凭据）
    assert SECRET not in summary["protocol_output"]
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr
