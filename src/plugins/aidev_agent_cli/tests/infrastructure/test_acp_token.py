# -*- coding: utf-8 -*-
"""ACP token 契约：env 优先、缺失即抛、**注入不落盘**、从不记取值、不接受 argv。

迁自原 worker ``test_config.py::TestResolveAccessToken``（8 例语义），并按当前契约新增
「注入不落盘」两例。这里的断言是 ACP 启动路径的安全边界：一次被拉起的服务端进程不该
改写用户的交互式登录态，也不该把客户端凭据写进任何日志。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aidev_agent_cli.infrastructure.settings import ACP_ACCESS_TOKEN_ENV, TuiConfig, resolve_acp_access_token


@pytest.fixture
def config_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把 TUI 配置重定向到一个临时文件（单文件语义）。"""
    path = tmp_path / "bkai_tui.json"
    monkeypatch.setenv(TuiConfig.CONFIG_FILE_OVERRIDE, str(path))
    monkeypatch.delenv(ACP_ACCESS_TOKEN_ENV, raising=False)

    return path


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ============================================================================
# 解析顺序
# ============================================================================


def test_env_token_wins_over_the_file_token(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """客户端声明的身份必须胜过磁盘上残留的交互登录态。"""
    _write(config_file, {"auth": {"access_token": "FROM-FILE"}})
    monkeypatch.setenv(ACP_ACCESS_TOKEN_ENV, "FROM-ENV")

    assert resolve_acp_access_token() == "FROM-ENV"


def test_blank_env_does_not_shadow_the_file_token(config_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空 env 不遮蔽文件（与 ``get_credentials`` 的 ``or`` 链同形）。"""
    _write(config_file, {"auth": {"access_token": "FROM-FILE"}})
    monkeypatch.setenv(ACP_ACCESS_TOKEN_ENV, "   ")

    assert resolve_acp_access_token() == "FROM-FILE"


def test_file_token_is_used_when_env_is_absent(config_file: Path) -> None:
    _write(config_file, {"auth": {"access_token": "FROM-FILE"}})

    assert resolve_acp_access_token() == "FROM-FILE"


def test_missing_token_raises_and_names_only_the_env_var(config_file: Path) -> None:
    """错误消息只点名环境变量——绝不包含任何凭据取值。"""
    with pytest.raises(RuntimeError) as caught:
        resolve_acp_access_token()

    assert ACP_ACCESS_TOKEN_ENV in str(caught.value)


def test_an_explicit_config_object_skips_the_disk(config_file: Path) -> None:
    """调用方给出的实例就是唯一来源：不再读盘（否则两个来源会分叉）。"""
    _write(config_file, {"auth": {"access_token": "FROM-FILE"}})

    assert resolve_acp_access_token(TuiConfig(access_token="EXPLICIT")) == "EXPLICIT"


def test_an_empty_explicit_config_object_yields_the_env_error(config_file: Path) -> None:
    with pytest.raises(RuntimeError):
        resolve_acp_access_token(TuiConfig())


def test_token_resolution_logs_the_source_but_never_the_value(
    config_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """日志只记来源（env / file）——这是凭据不入日志的唯一落点。

    断言挂在**真实 handler** 而不是 ``caplog``：``configure_logging`` 把
    ``aidev_agent_cli`` 命名空间设成 ``propagate = False``（避免与宿主重复输出），
    因此 ``caplog`` 收不到。往命名空间上挂一个只给本用例用的记录 handler，测的就是
    线上那个 logger 实际发出的内容。
    """
    import logging

    records: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    _write(config_file, {"auth": {"access_token": "SECRET-FILE-TOK"}})
    monkeypatch.setenv(ACP_ACCESS_TOKEN_ENV, "SECRET-ENV-TOK")

    namespace = logging.getLogger("aidev_agent_cli")
    collector = _Collector()
    namespace.addHandler(collector)
    try:
        assert resolve_acp_access_token() == "SECRET-ENV-TOK"
    finally:
        namespace.removeHandler(collector)

    joined = "\n".join(records)
    assert "env" in joined
    assert "SECRET-ENV-TOK" not in joined
    assert "SECRET-FILE-TOK" not in joined


def test_an_unreadable_config_file_degrades_to_missing_token(config_file: Path) -> None:
    """坏文件不是崩溃：按「没有 token」处理，由入口给出可操作的提示。"""
    config_file.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError):
        resolve_acp_access_token()


# ============================================================================
# argv / repr 安全边界
# ============================================================================


def test_the_token_is_never_accepted_from_argv() -> None:
    """ACP 入口**不**提供 ``--token``：进程列表对同机用户可读。

    断言的是 argparse 面（唯一能接受 argv 的地方）：只有 ``--protocol`` /
    ``--resources``，没有任何 token 选项。
    """
    from aidev_agent_cli.__main__ import _parser

    options = {action.dest for action in _parser()._actions}
    assert "token" not in options
    assert "access_token" not in options


def test_the_config_repr_never_echoes_the_token(config_file: Path) -> None:
    """``repr`` 是日志/调试最常见的意外泄漏点。"""
    _write(config_file, {"auth": {"access_token": "SECRET-TOK"}})

    text = repr(TuiConfig.read())

    assert "SECRET-TOK" not in text
    assert "access_token=set" in text
