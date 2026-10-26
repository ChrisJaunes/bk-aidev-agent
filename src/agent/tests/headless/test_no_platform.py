# -*- coding: utf-8 -*-
"""T-10：无平台依赖与隔离验证（REQ-8 / REQ-11 / ACC-7 / ACC-10）。

子进程内完成一轮纯文本对话后断言：
- ``sys.modules`` 中不存在 ``django`` / ``blueapps``；
- ``HeadlessResourceManager.get_client`` 计数桩为 0（平台调用为 0）；
- 注入的 ``auth_headers`` 值不出现在 stdout / stderr；
- headless 子包源码不出现 django / blueapps 导入。
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.headless._fake_llm import FakeLLMServer

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HEADLESS_DIR = PROJECT_ROOT / "aidev_agent" / "headless"
SECRET = "AUTH-HEADER-SECRET-VALUE"

CHILD_SCRIPT = textwrap.dedent(
    """
    import io, json, sys

    from aidev_agent.headless import resource_manager as rm_module
    from aidev_agent.headless.cli import Dispatcher
    from aidev_agent.headless.protocol import Request

    calls = {"get_client": 0}
    _original = rm_module.HeadlessResourceManager.get_client

    def _counting_get_client(self, **kwargs):
        calls["get_client"] += 1
        return _original(self, **kwargs)

    rm_module.HeadlessResourceManager.get_client = _counting_get_client

    base_url, model, secret = sys.argv[1], sys.argv[2], sys.argv[3]
    out = io.StringIO()
    dispatcher = Dispatcher(stream=out)
    dispatcher.handle_request(
        Request(
            id="c1",
            method="create",
            params={
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": base_url, "model": model, "auth_headers": {"X-Bkapi-Authorization": secret}},
            },
        )
    )
    dispatcher.handle_request(Request(id="t1", method="turn", params={"session_code": "s1", "input": "hi"}))
    dispatcher.join(60)

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    response = [line for line in lines if line.get("id") == "t1" and "ok" in line]
    summary = {
        "status": response[0]["result"]["status"] if response else "missing",
        "platform_calls": calls["get_client"],
        "django_modules": sorted(m for m in sys.modules if m == "django" or m.startswith("django.")),
        "blueapps_modules": sorted(m for m in sys.modules if m == "blueapps" or m.startswith("blueapps.")),
        "protocol_output": out.getvalue(),
    }
    sys.stderr.write("SUMMARY=" + json.dumps(summary) + "\\n")
    """
)


@pytest.fixture
def llm():
    with FakeLLMServer(["离线一轮"]) as server:
        yield server


def test_one_turn_runs_without_platform(llm, tmp_path):
    script = tmp_path / "child.py"
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

    assert summary["status"] == "finished"
    assert summary["django_modules"] == []
    assert summary["blueapps_modules"] == []
    assert summary["platform_calls"] == 0, summary["platform_calls"]
    assert SECRET not in summary["protocol_output"]
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_headless_sources_do_not_import_platform_frameworks():
    offenders = []
    for path in HEADLESS_DIR.glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "django" in stripped or "blueapps" in stripped:
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_headless_package_has_no_new_dependencies():
    """headless 只用现有依赖：不引入 dsh 相关包，也不改依赖声明。"""
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "deepseek" not in pyproject.lower()
    assert "dsh" not in pyproject.lower()
