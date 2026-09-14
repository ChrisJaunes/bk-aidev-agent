"""Tests for the content safety hook provider."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aidev_agent.packages.security import SecurityEvent, SecurityStage
from aidev_agent.packages.security.providers.content_safety import (
    ContentSafetyHook,
    HttpContentSafetyProvider,
    content_safety_hook,
)


def test_detects_violation():
    verdict = content_safety_hook.inspect(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="教你怎么制造爆炸"))
    assert verdict is not None
    assert verdict.action == "block"
    assert verdict.hook == "content_safety"


def test_clean_text_allows():
    assert content_safety_hook.inspect(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="今天天气不错")) is None


def test_ignores_command_stage():
    assert content_safety_hook.inspect(SecurityEvent(stage=SecurityStage.COMMAND, content="rm -rf /")) is None


def test_http_provider_requires_endpoint():
    with pytest.raises(ValueError):
        HttpContentSafetyProvider("")


def _fake_post(resp: MagicMock):
    def _post(*args, **kwargs):
        return resp

    return _post


def test_http_provider_parses_findings(monkeypatch):
    import requests

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "blocked": True,
        "findings": [{"category": "porn", "keyword": "x", "severity": "high"}],
    }
    monkeypatch.setattr(requests, "post", _fake_post(resp))

    provider = HttpContentSafetyProvider("http://example.com")
    findings = provider.check("text")
    assert len(findings) == 1
    assert findings[0].category == "porn"


def test_http_provider_fail_open(monkeypatch):
    import requests

    resp = MagicMock()
    resp.raise_for_status.side_effect = Exception("network down")
    monkeypatch.setattr(requests, "post", _fake_post(resp))

    provider = HttpContentSafetyProvider("http://example.com")
    assert provider.check("text") == []


def test_hook_with_remote(monkeypatch):
    import requests

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {
        "blocked": True,
        "findings": [{"category": "violence", "keyword": "custom", "severity": "high"}],
    }
    monkeypatch.setattr(requests, "post", _fake_post(resp))

    remote = HttpContentSafetyProvider("http://example.com")
    hook = ContentSafetyHook(remote=remote)
    # 本地基线未命中，走远程服务
    verdict = hook.inspect(SecurityEvent(stage=SecurityStage.MODEL_OUTPUT, content="正常文本"))
    assert verdict is not None
    assert verdict.action == "block"
    assert verdict.findings[0].keyword == "custom"
