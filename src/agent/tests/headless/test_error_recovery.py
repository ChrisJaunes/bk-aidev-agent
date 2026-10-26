# -*- coding: utf-8 -*-
"""T-9：错误处理与进程可用性（REQ-5 / REQ-10 / ACC-13）。

六类坏请求 —— 非法 JSON、未知方法、未知会话、``close`` 后请求、装配异常、
执行期未归类异常 —— 都必须返回结构化错误、写出 ``RUN_ERROR``，且进程/连接继续可用。
错误响应不得包含完整请求体与凭据。
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from ag_ui.core import EventType
from aidev_agent.headless.session import SessionConfig
from aidev_agent.services.agent.factory import DefaultAgentFactory, agent_factory

from tests.headless._fake_llm import FakeLLMServer
from tests.headless._harness import Harness

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SECRET = "SECRET-TOKEN-DO-NOT-LEAK"


@pytest.fixture
def llm():
    with FakeLLMServer(["好的"]) as server:
        yield server


def _run_headless(stdin_text: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
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


def test_six_bad_requests_keep_the_connection_usable(llm):
    create_ok = {
        "id": "ok-create",
        "method": "create",
        "params": {
            "session_code": "s1",
            "agent": {"agent_code": "agent-hook"},
            "llm": {
                "base_url": llm.base_url,
                "model": "fake-model",
                "auth_headers": {"X-Bkapi-Authorization": SECRET},
            },
        },
    }
    lines = [
        "{broken json",
        json.dumps({"id": "bad-method", "method": "teleport", "params": {}}),
        json.dumps({"id": "bad-session", "method": "turn", "params": {"session_code": "ghost"}}),
        json.dumps(create_ok),
        json.dumps({"id": "ok-close", "method": "close", "params": {"session_code": "s1"}}),
        json.dumps({"id": "after-close", "method": "turn", "params": {"session_code": "s1", "input": "hi"}}),
        json.dumps(
            {
                "id": "bad-build",
                "method": "create",
                "params": {"session_code": "s2", "agent": {"agent_code": "agent-hook"}, "llm": {"model": ""}},
            }
        ),
        json.dumps(
            {
                "id": "ok-turn",
                "method": "create",
                "params": {
                    "session_code": "s3",
                    "agent": {"agent_code": "agent-hook"},
                    "llm": {"base_url": llm.base_url, "model": "fake-model"},
                },
            }
        ),
        json.dumps({"id": "ok-turn-2", "method": "turn", "params": {"session_code": "s3", "input": "还活着吗"}}),
    ]
    result = _run_headless("\n".join(lines) + "\n")
    assert result.returncode == 0, result.stderr[-3000:]

    parsed = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    responses = {item["id"]: item for item in parsed if "ok" in item}

    assert responses[""]["error"]["code"] == "protocol.invalid_json"
    assert responses["bad-method"]["error"]["code"] == "protocol.unknown_method"
    assert responses["bad-session"]["error"]["code"] == "session.not_found"
    assert responses["after-close"]["error"]["code"] == "session.closed"
    assert responses["bad-build"]["error"]["code"] == "agent.build_failed"
    # 末尾合法请求正常完成 → 进程仍可用
    assert responses["ok-turn-2"]["ok"] is True
    assert responses["ok-turn-2"]["result"]["status"] == "finished"

    # 每条失败请求都在自己 id 下写出 RUN_ERROR
    for bad_id in ("", "bad-method", "bad-session", "after-close", "bad-build"):
        types = [
            item["event"]["type"]
            for item in parsed
            if item.get("type") == "event" and item.get("id") == bad_id
        ]
        assert EventType.RUN_ERROR.value in types, (bad_id, types)

    # 凭据与请求体不得出现在任何输出里
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_runtime_exception_maps_to_internal_error_and_process_stays_usable(llm):
    class _BoomFactory(DefaultAgentFactory):
        def create(self, **build_params):
            agent = super().create(**build_params)

            class _BoomAgent:
                def __init__(self, inner):
                    self._inner = inner

                def execute(self, execute_kwargs):
                    raise RuntimeError("kaboom")

                def stop(self):
                    return None

            return _BoomAgent(agent)

    legacy = agent_factory.replace_defaults(_BoomFactory())
    try:
        harness = Harness()
        harness.send_obj(
            "c1",
            "create",
            {
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": llm.base_url, "model": "fake-model"},
            },
        )
        harness.result("c1")
        harness.send("t1", "turn", session_code="s1", input="hi")
        harness.wait()
        assert harness.error("t1")["code"] == "agent.internal_error"
        assert harness.event_types("t1")[-1] == EventType.RUN_ERROR.value
    finally:
        agent_factory.replace_defaults(legacy)


def test_model_5xx_returns_structured_failure_without_duplicate_run_error():
    """执行链只产出 RUN_ERROR 事件（不抛异常）时，响应必须 ok=false 且带稳定错误码。

    F-1 回归：模型端点 5xx / 不可达时，平台侧只发 RUN_ERROR + 补发 RUN_FINISHED(success)，
    不抛出到 headless。修复前该轮以 ``ok=true`` + ``status=error`` + 空回复结束，
    违反 REQ-5（不得返回空回复）与 REQ-7（三态可区分）。
    """
    with FakeLLMServer(["不会用到"], error_status=500, error_message="upstream model is unavailable") as broken:
        harness = Harness()
        harness.send_obj(
            "c1",
            "create",
            {
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": broken.base_url, "model": "fake-model"},
            },
        )
        harness.result("c1")

        harness.send("t1", "turn", session_code="s1", input="hi")
        harness.wait()

        error = harness.error("t1")
        assert error["code"] == "agent.internal_error"
        assert "upstream model is unavailable" in error["message"]

        # 基线事件序列不变：RUN_ERROR 后由平台补发 RUN_FINISHED(success)，且只写一条 RUN_ERROR
        types = harness.event_types("t1")
        assert types[0] == EventType.RUN_STARTED.value
        assert types[-2:] == [EventType.RUN_ERROR.value, EventType.RUN_FINISHED.value]
        assert types.count(EventType.RUN_ERROR.value) == 1, types
        terminal = harness.events("t1")[-1]
        assert terminal["outcome"]["type"] == "success"

        # resume 同形：同一会话续轮遇到同样的执行期错误，同样是 ok=false + 稳定错误码
        harness.send("r1", "resume", session_code="s1", input="again")
        harness.wait()
        resume_error = harness.error("r1")
        assert resume_error["code"] == "agent.internal_error"
        assert "upstream model is unavailable" in resume_error["message"]
        resume_types = harness.event_types("r1")
        assert resume_types.count(EventType.RUN_ERROR.value) == 1, resume_types
        assert resume_types[-2:] == [EventType.RUN_ERROR.value, EventType.RUN_FINISHED.value]


def test_build_error_at_create_does_not_leave_a_broken_session(llm):
    harness = Harness()
    harness.send_obj(
        "c1",
        "create",
        {"session_code": "s1", "agent": {"agent_code": "agent-hook"}, "llm": {"model": ""}},
    )
    assert harness.error("c1")["code"] == "agent.build_failed"
    # 失败后同名会话可重新创建（不残留半成品）
    harness.send_obj(
        "c2",
        "create",
        {
            "session_code": "s1",
            "agent": {"agent_code": "agent-hook"},
            "llm": {"base_url": llm.base_url, "model": "fake-model"},
        },
    )
    assert harness.result("c2")["created"] is True
