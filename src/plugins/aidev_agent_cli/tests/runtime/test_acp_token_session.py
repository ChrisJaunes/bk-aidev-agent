# -*- coding: utf-8 -*-
"""``runtime.auth.AuthManager.login`` 的 token 落盘语义（主测 ``runtime``）。

入口侧的 ACP token 解析契约在 ``tests/runtime/test_acp_token.py``；本文件承载
「ACP 注入的 token 不覆盖交互式登录态」这一身份层的安全边界。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aidev_agent_cli.runtime.settings import BkAiConfig
from aidev_agent_cli.runtime.auth import AuthManager


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 TUI 配置重定向到一个临时文件。"""
    path = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(BkAiConfig.CONFIG_FILE_OVERRIDE, str(path))
    monkeypatch.delenv(BkAiConfig.CWD_ENV, raising=False)

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

    auth = AuthManager(BkAiConfig.read())
    auth.login("INJECTED-TOK", persist=False)

    assert auth.has_token() is True
    assert config_file.read_text(encoding="utf-8") == original
    assert json.loads(config_file.read_text(encoding="utf-8"))["auth"]["access_token"] == "INTERACTIVE-TOK"


def test_persist_true_still_writes_the_interactive_login_state(config_file: Path) -> None:
    """交互式 ``/login`` 的现状逐字保留：``persist=True`` 才写盘。"""
    _write(config_file, {"auth": {"access_token": "OLD-TOK"}})

    auth = AuthManager(BkAiConfig.read())
    auth.login("NEW-TOK", persist=True)

    assert json.loads(config_file.read_text(encoding="utf-8"))["auth"]["access_token"] == "NEW-TOK"