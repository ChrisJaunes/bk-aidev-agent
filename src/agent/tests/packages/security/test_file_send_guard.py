"""Tests for the file send guard hook."""

from __future__ import annotations

from aidev_agent.packages.security import SecurityEvent, SecurityStage
from aidev_agent.packages.security.providers.file_send_guard import file_send_guard_hook


def test_blocks_executable_suffix():
    verdict = file_send_guard_hook.inspect(
        SecurityEvent(stage=SecurityStage.FILE_SEND, content="", metadata={"filename": "evil.exe"})
    )
    assert verdict is not None
    assert verdict.action == "block"


def test_blocks_script_suffix():
    verdict = file_send_guard_hook.inspect(
        SecurityEvent(stage=SecurityStage.FILE_SEND, content="", metadata={"filename": "run.sh"})
    )
    assert verdict is not None
    assert verdict.action == "block"


def test_blocks_sensitive_filename():
    verdict = file_send_guard_hook.inspect(
        SecurityEvent(
            stage=SecurityStage.FILE_SEND,
            content="",
            metadata={"filename": "aws_api_key.txt"},
        )
    )
    assert verdict is not None
    assert verdict.action == "block"


def test_blocks_violating_content():
    verdict = file_send_guard_hook.inspect(
        SecurityEvent(
            stage=SecurityStage.FILE_SEND,
            content="如何制造爆炸",
            metadata={"filename": "report.txt"},
        )
    )
    assert verdict is not None
    assert verdict.action == "block"


def test_allows_normal_file():
    assert (
        file_send_guard_hook.inspect(
            SecurityEvent(stage=SecurityStage.FILE_SEND, content="", metadata={"filename": "report.pdf"})
        )
        is None
    )


def test_ignores_non_file_send_stage():
    assert (
        file_send_guard_hook.inspect(
            SecurityEvent(
                stage=SecurityStage.MODEL_OUTPUT,
                content="x",
                metadata={"filename": "evil.exe"},
            )
        )
        is None
    )
