# -*- coding: utf-8 -*-
"""T-10：无平台依赖与隔离验证（REQ-8 / REQ-11 / ACC-7 / ACC-10）。

子进程内完成一轮纯文本对话后断言：
- ``sys.modules`` 中不存在 ``django`` / ``blueapps``；
- ``LocalResourceManager.get_client``（注入模式） 计数桩为 0（平台调用为 0）；
- 注入的 ``auth_headers`` 值不出现在 stdout / stderr；
- CLI 包源码不出现 django / blueapps 导入。
"""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from tests.integration._fake_llm import FakeLLMServer
from tests.integration._paths import AGENT_ROOT, PLUGIN_ROOT

PROJECT_ROOT = AGENT_ROOT
#: 迁出后源码在插件包里，不在 SDK 下。
PKG_DIR = PLUGIN_ROOT / "aidev_agent_cli"
SECRET = "AUTH-HEADER-SECRET-VALUE"

CHILD_SCRIPT = textwrap.dedent(
    """
    import io, json, sys

    from tests.integration._worker_observations import WorkerRecords
    from aidev_agent_cli.resources import manager as rm_module
    from tests.integration._driver import AguiAdapter
    from tests.integration._driver import Request

    calls = {"get_client": 0}
    _original = rm_module.LocalResourceManager.get_client

    def _counting_get_client(self, **kwargs):
        calls["get_client"] += 1
        return _original(self, **kwargs)

    rm_module.LocalResourceManager.get_client = _counting_get_client

    base_url, model, secret = sys.argv[1], sys.argv[2], sys.argv[3]
    out = io.StringIO()
    dispatcher = AguiAdapter(stream=out)
    dispatcher.handle_request(
        Request(
            id="c1",
            method="create",
            params={
                "session_code": "s1",
                "agent": {"agent_code": "agent-hook"},
                "llm": {"base_url": base_url, "model": model, "auth_headers": {"X-Bkapi-Authorization": secret}},
                "tool_factories": ["tests.integration._worker_observations:probe_tool_factory"],
            },
        )
    )
    dispatcher.handle_request(Request(id="t1", method="turn", params={"session_code": "s1", "input": "hi"}))
    dispatcher.join(60)

    lines = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    response = [line for line in lines if line.get("id") == "t1" and "ok" in line]
    summary = {
        "status": response[0]["result"]["status"] if response else "missing",
        "platform_calls": sum(r.get("call") == "get_client" for r in WorkerRecords("RUNTIME_TEST_PLATFORM")),
        "django_modules": sorted({m for r in WorkerRecords("RUNTIME_TEST_PLATFORM") for m in r.get("modules", []) if m == "django" or m.startswith("django.")}),
        "blueapps_modules": sorted({m for r in WorkerRecords("RUNTIME_TEST_PLATFORM") for m in r.get("modules", []) if m == "blueapps" or m.startswith("blueapps.")}),
        "worker_observations": sum("modules" in r for r in WorkerRecords("RUNTIME_TEST_PLATFORM")),
        "protocol_output": out.getvalue(),
    }
    sys.stderr.write("SUMMARY=" + json.dumps(summary) + "\\n")
    """
)


@pytest.fixture
def llm():
    with FakeLLMServer(["离线一轮"]) as server:
        yield server


@pytest.mark.slow
def test_one_turn_runs_without_platform(llm, tmp_path):
    script = tmp_path / "child.py"
    script.write_text("def main():\n" + textwrap.indent(CHILD_SCRIPT, "    ") + "\nif __name__ == '__main__':\n    main()\n", encoding="utf-8")
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    # 子进程按**脚本路径**起跑（cwd 是 SDK 根），``tests.*`` 只能靠 PYTHONPATH 解析。
    env["PYTHONPATH"] = os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), env.get("PYTHONPATH", "")])
    env["RUNTIME_TEST_PLATFORM"] = str(tmp_path / "platform.ndjson")
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
    assert summary["worker_observations"] >= 2, "必须观测 prepare 和执行 worker 的真实模块"
    assert summary["django_modules"] == []
    assert summary["blueapps_modules"] == []
    assert summary["platform_calls"] == 0, summary["platform_calls"]
    assert SECRET not in summary["protocol_output"]
    assert SECRET not in result.stdout
    assert SECRET not in result.stderr


def test_headless_sources_do_not_import_platform_frameworks():
    """扫描**整包**源码（含四个子包），确认没有 django / blueapps 导入。

    必须用 ``rglob``：模块已按职责迁入 ``embedded`` / ``protocols`` / ``tui`` /
    ``infrastructure`` 四个子包，顶层只剩 ``__init__`` 与 ``__main__``。若退回
    ``glob("*.py")``，只会扫到那两个文件——django / blueapps 检查会静默通过，
    而这条用例的全部价值就在「真的扫到了」。
    """
    offenders = []
    for path in PKG_DIR.rglob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            if "django" in stripped or "blueapps" in stripped:
                offenders.append(f"{path.name}: {stripped}")
    assert offenders == []


def test_headless_package_has_no_new_dependencies():
    """CLI 包只用现有依赖：不引入 dsh 相关包，也不改依赖声明。

    SDK 已无 headless extra，因此改读插件自己的 pyproject。
    """
    pyproject = (PLUGIN_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "deepseek" not in pyproject.lower()
    assert "dsh" not in pyproject.lower()