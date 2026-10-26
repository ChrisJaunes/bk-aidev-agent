# -*- coding: utf-8 -*-
"""T-5：NDJSON 协议层与入口骨架（REQ-6 / REQ-11）。

验证点：
- ``python -m aidev_agent.headless`` 可启动，stdin EOF 时返回码 0；
- 请求 / 事件 / 响应三种行模型就位，事件行在响应行之前，均带请求 ``id``；
- stdout 只有协议数据（每行都是 JSON），stderr 不含协议行；
- ``protocol.invalid_json`` / ``protocol.unknown_method`` / ``protocol.invalid_params``
  三个错误码可用，且每条请求恰好一条响应。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.headless._harness import Harness

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # src/agent


def _run_headless(stdin_text: str, timeout: float = 120.0) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    env.pop("RABBITMQ_HOST", None)
    env.pop("REDIS_URL", None)
    return subprocess.run(
        [sys.executable, "-m", "aidev_agent.headless"],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def test_entry_exits_zero_on_eof():
    result = _run_headless("")
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ""


def test_protocol_errors_are_structured_and_stdout_stays_clean():
    lines = [
        "{not json",
        json.dumps({"id": "m1", "method": "explode", "params": {}}),
        json.dumps({"id": "m2", "method": "turn", "params": {}}),
        json.dumps({"id": "m3", "method": "turn", "params": {"session_code": "missing"}}),
    ]
    result = _run_headless("\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr[-2000:]

    stdout_lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert stdout_lines, "stdout 不应为空"
    parsed = [json.loads(line) for line in stdout_lines]  # 每行都是协议 JSON
    for item in parsed:
        assert set(item) & {"type", "ok"}, item

    # stderr 只承载日志，不得出现协议行
    for line in result.stderr.splitlines():
        assert not line.strip().startswith('{"id"'), f"stderr 出现协议行：{line[:120]}"

    responses = {item["id"]: item for item in parsed if "ok" in item}
    assert responses[""]["error"]["code"] == "protocol.invalid_json"
    assert responses["m1"]["error"]["code"] == "protocol.unknown_method"
    assert responses["m2"]["error"]["code"] == "protocol.invalid_params"
    assert responses["m3"]["error"]["code"] == "session.not_found"


def test_events_precede_response_and_carry_request_id():
    harness = Harness()
    harness.send("e1", "turn", session_code="nope")
    harness.wait()

    lines = harness.lines()
    assert len(lines) == 2, lines
    event_line, response_line = lines
    assert event_line["type"] == "event"
    assert event_line["id"] == "e1"
    assert event_line["event"]["type"] == "RUN_ERROR"
    assert response_line["id"] == "e1" and response_line["ok"] is False


def test_event_line_serialises_ag_ui_event_by_alias():
    from ag_ui.core import RunErrorEvent

    from aidev_agent.headless.protocol import encode_event

    line = json.loads(encode_event("r1", RunErrorEvent(message="boom")))
    assert line["id"] == "r1"
    assert line["type"] == "event"
    assert line["event"]["type"] == "RUN_ERROR"
    assert line["event"]["message"] == "boom"
