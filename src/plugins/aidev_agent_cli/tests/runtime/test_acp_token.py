# -*- coding: utf-8 -*-
"""ACP 身份契约：与其他协议同一条组装路径，token 只来自 ``bkai_tui.json``。

不接受 env / argv 注入，``repr`` 不回显取值。这里的断言是 ACP 启动路径的安全边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aidev_agent_cli import __main__ as entry
from aidev_agent_cli.runtime.settings import BkAiConfig


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 TUI 配置重定向到一个临时文件。"""
    path = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(BkAiConfig.CONFIG_FILE_OVERRIDE, str(path))
    monkeypatch.delenv(BkAiConfig.CWD_ENV, raising=False)

    return path


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list:
    """跑入口但不真正起服务：捕获交给 ``build_runtime`` 的 ``(protocol, ProcessConfig)``。"""
    calls: list = []
    monkeypatch.setattr(entry, "install_startup_hooks", lambda env: None)
    monkeypatch.setattr(entry, "resolve_adapter", lambda protocol: lambda service, channel: 0)
    monkeypatch.setattr(entry.ProtocolChannel, "stdio", classmethod(lambda cls: None))

    def build_runtime(protocol, config, hooks=None):
        calls.append((protocol, config))
        return object()

    monkeypatch.setattr(entry, "build_runtime", build_runtime)
    return calls


@pytest.mark.parametrize("protocol", ["acp", "acp-dsh", "agui"])
def test_every_protocol_takes_the_token_from_the_config_file(
    config_file: Path, started: list, monkeypatch: pytest.MonkeyPatch, protocol: str
) -> None:
    """所有协议同一条组装路径：身份只来自配置文件，环境变量不参与。"""
    config_file.write_text(json.dumps({"auth": {"access_token": "FROM-FILE"}}), encoding="utf-8")
    monkeypatch.setenv("BKAI_TUI_ACCESS_TOKEN", "FROM-ENV")

    assert entry.main(["--protocol", protocol, "--resources", "platform"]) == 0
    assert started[0][1].auth.access_token == "FROM-FILE"


def test_the_token_is_never_accepted_from_argv() -> None:
    """入口**不**提供 ``--token``：进程列表对同机用户可读。"""
    options = {action.dest for action in entry._parser()._actions}
    assert "token" not in options
    assert "access_token" not in options


def test_the_config_repr_never_echoes_the_token(config_file: Path) -> None:
    """``repr`` 是日志/调试最常见的意外泄漏点。"""
    config_file.write_text(json.dumps({"auth": {"access_token": "SECRET-TOK"}}), encoding="utf-8")

    text = repr(BkAiConfig.read())

    assert "SECRET-TOK" not in text
    assert "access_token=set" in text
