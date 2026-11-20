# -*- coding: utf-8 -*-
"""双入口 smoke：``python -m aidev_agent_cli`` 的两种协议，以及旧入口必须消失。"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.integration._paths import AGENT_ROOT, PLUGIN_ROOT, PYTHON


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env["MESSAGE_HANDLER_TYPE"] = "inmemory"
    env.pop("RABBITMQ_HOST", None)
    env.pop("REDIS_URL", None)
    # 未安装 wheel 时两侧都要可解析
    env["PYTHONPATH"] = os.pathsep.join([str(PLUGIN_ROOT), str(AGENT_ROOT), env.get("PYTHONPATH", "")])
    return env


def _run(args: list[str], stdin_text: str = "", timeout: float = 120.0) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, "-u", *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(AGENT_ROOT),
        env=_env(),
    )


@pytest.mark.slow
def test_embedded_entry_exits_zero_on_eof():
    result = _run(["-m", "aidev_agent_cli"])
    assert result.returncode == 0, result.stderr[-2000:]
    assert result.stdout.strip() == ""


@pytest.mark.slow
def test_embedded_entry_answers_one_request_with_one_response():
    """默认协议是 acp-dsh：一条 initialize 得到一条 JSON-RPC 响应，stderr 不混协议帧。"""
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": 1}})
    result = _run(["-m", "aidev_agent_cli"], request + "\n")
    assert result.returncode == 0, result.stderr[-2000:]

    parsed = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    responses = [item for item in parsed if "result" in item or "error" in item]
    assert len(responses) == 1, result.stdout
    assert responses[0]["id"] == 1
    assert responses[0]["result"]["_meta"]["aidev"]["protocol"] == "acp-dsh"
    for line in result.stderr.splitlines():
        assert not line.strip().startswith('{"jsonrpc"'), f"stderr 出现协议行：{line[:120]}"


@pytest.mark.slow
def test_tui_entry_emits_ready_then_result():
    request = json.dumps({"id": "s", "method": "process.shutdown", "params": {}})
    result = _run(["-m", "aidev_agent_cli", "--protocol", "tui"], request + "\n")
    assert result.returncode == 0, result.stderr[-2000:]

    kinds = [json.loads(line)["kind"] for line in result.stdout.splitlines() if line.strip()]
    assert kinds[:2] == ["ready", "result"], kinds


def test_legacy_entry_is_gone():
    """不提供 ``python -m aidev_agent.headless`` 兼容 shim。"""
    result = _run(["-m", "aidev_agent.headless"])
    assert result.returncode != 0
    assert "No module named aidev_agent.headless" in result.stderr


def test_package_level_api_is_callable_directly():
    import aidev_agent_cli

    assert callable(aidev_agent_cli.main)
    assert not hasattr(aidev_agent_cli, "serve"), "headless 的 serve 已随协议一起删除"
