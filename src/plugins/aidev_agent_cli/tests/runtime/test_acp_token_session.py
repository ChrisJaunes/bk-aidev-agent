# -*- coding: utf-8 -*-
"""``runtime.sdk.session.SDKSession.auth.login`` 的 token 落盘语义（主测 ``runtime``）。

从 ``tests/infrastructure/test_acp_token.py`` 按断言目标拆出：那一侧只留
``infrastructure.settings.resolve_acp_access_token`` 的解析/日志/argv/repr 契约，本文件
承载「ACP 注入的 token 不覆盖交互式登录态」这一 ``Session`` 层的安全边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aidev_agent_cli.infrastructure.settings import ACP_ACCESS_TOKEN_ENV, TuiConfig


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 TUI 配置重定向到一个临时文件（单文件语义）。"""
    path = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(TuiConfig.CONFIG_FILE_OVERRIDE, str(path))
    monkeypatch.delenv(ACP_ACCESS_TOKEN_ENV, raising=False)

    return path


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_persist_false_updates_memory_but_leaves_the_file_untouched(config_file: Path) -> None:
    """**不落盘回归**：ACP 注入的 token 不得覆盖交互式登录态。

    ``RuntimeService(..., persist_token=False)`` 最终走到
    ``Session.auth.login(token, persist=False)``；这条用例从 Session
    这一层钉住「内存变了、文件没变」。
    """
    _write(config_file, {"auth": {"access_token": "INTERACTIVE-TOK"}})
    original = config_file.read_text(encoding="utf-8")

    from aidev_agent_cli.runtime.auth import AuthManager

    auth = AuthManager()
    auth.login("INJECTED-TOK", persist=False)

    assert auth.has_token() is True
    assert config_file.read_text(encoding="utf-8") == original
    assert json.loads(config_file.read_text(encoding="utf-8"))["auth"]["access_token"] == "INTERACTIVE-TOK"


def test_persist_true_still_writes_the_interactive_login_state(config_file: Path) -> None:
    """交互式 ``/login`` 的现状逐字保留：``persist=True`` 才写盘。"""
    _write(config_file, {"auth": {"access_token": "OLD-TOK"}})

    from aidev_agent_cli.runtime.auth import AuthManager

    auth = AuthManager()
    auth.login("NEW-TOK", persist=True)

    assert json.loads(config_file.read_text(encoding="utf-8"))["auth"]["access_token"] == "NEW-TOK"
