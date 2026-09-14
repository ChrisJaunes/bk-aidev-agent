# -*- coding: utf-8 -*-
"""Tests for SecuritySettings (aidev_agent.pydantic_models) + resource_manager 下发通道。"""

from __future__ import annotations

import pytest
from aidev_agent.packages.resource_manager.base import BaseResourceManager
from aidev_agent.pydantic_models import _MANDATORY_REDACT_FIELDS, SecuritySettings


class TestSecuritySettingsFieldDefaults:
    """字段级 ``default_factory`` 环境变量兜底（from_mapping 之下的最后一层默认）。"""

    def test_default_true(self, monkeypatch):
        monkeypatch.delenv("AIDEV_NETWORK_ALLOWLIST", raising=False)
        assert SecuritySettings().network_allowlist is True

    def test_default_false_for_approval(self, monkeypatch):
        monkeypatch.delenv("AIDEV_COMMAND_APPROVAL", raising=False)
        assert SecuritySettings().command_approval is False

    def test_env_disable(self, monkeypatch):
        monkeypatch.setenv("AIDEV_COMMAND_BLACKLIST", "false")
        assert SecuritySettings().command_blacklist is False

    def test_env_enable_approval(self, monkeypatch):
        monkeypatch.setenv("AIDEV_COMMAND_APPROVAL", "true")
        assert SecuritySettings().command_approval is True


class TestSecuritySettingsStringConfig:
    def test_default_empty(self, monkeypatch):
        monkeypatch.delenv("AIDEV_ALLOW_DOMAINS", raising=False)
        assert SecuritySettings().network_allow_domains == ""

    def test_env_value(self, monkeypatch):
        monkeypatch.setenv("AIDEV_ALLOW_DOMAINS", "example.com,*.foo.com")
        assert SecuritySettings().network_allow_domains == "example.com,*.foo.com"


class TestSecuritySettingsAllFields:
    def test_returns_all_fields(self, monkeypatch):
        monkeypatch.delenv("AIDEV_NETWORK_ALLOWLIST", raising=False)
        monkeypatch.delenv("AIDEV_COMMAND_APPROVAL", raising=False)
        settings = SecuritySettings()
        assert settings.network_allowlist is True
        assert settings.command_approval is False
        assert settings.network_allow_domains == ""
        assert settings.command_approval_approvers == ""


class TestSecuritySettingsFromMapping:
    def test_mapping_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AIDEV_COMMAND_BLACKLIST", "true")
        settings = SecuritySettings.from_mapping({"command_blacklist": False})
        assert settings.command_blacklist is False  # 平台值覆盖环境变量

    def test_mapping_missing_falls_back_to_env(self, monkeypatch):
        monkeypatch.delenv("AIDEV_COMMAND_BLACKLIST", raising=False)
        settings = SecuritySettings.from_mapping({})
        assert settings.command_blacklist is True  # 缺失字段 → 字段默认兜底

    def test_unknown_key_ignored(self):
        settings = SecuritySettings.from_mapping({"unknown_feature": True})
        assert "unknown_feature" not in settings.model_dump()

    def test_no_from_env_classmethod(self):
        """``from_env()`` 已删除：统一配置入口是 AgentConfig.security_settings。"""
        assert not hasattr(SecuritySettings, "from_env")


class TestPartialMaskThresholdsConfig:
    """T-urf-03：partial 掩码三个阈值字段（默认 32/6/4，env 可覆盖，平台可下发）。"""

    @pytest.mark.parametrize(
        "field, env_name, expected_default",
        [
            ("redact_partial_min_len", "AIDEV_REDACT_PARTIAL_MIN_LEN", 32),
            ("redact_partial_head", "AIDEV_REDACT_PARTIAL_HEAD", 6),
            ("redact_partial_tail", "AIDEV_REDACT_PARTIAL_TAIL", 4),
        ],
    )
    def test_default_from_env_fallback(self, monkeypatch, field, env_name, expected_default):
        monkeypatch.delenv(env_name, raising=False)
        assert getattr(SecuritySettings(), field) == expected_default

    @pytest.mark.parametrize(
        "field, env_name, env_value",
        [
            ("redact_partial_min_len", "AIDEV_REDACT_PARTIAL_MIN_LEN", 8),
            ("redact_partial_head", "AIDEV_REDACT_PARTIAL_HEAD", 2),
            ("redact_partial_tail", "AIDEV_REDACT_PARTIAL_TAIL", 5),
        ],
    )
    def test_mapping_override(self, field, env_name, env_value):
        """平台下发值经 from_mapping 覆盖字段默认（env 变量名仅作对照说明）。"""
        settings = SecuritySettings.from_mapping({field: env_value})
        assert getattr(settings, field) == env_value


_BASE_RAW = {
    "agent_name": "sec-agent",
    "prompt_setting": {"llm_code": "test-llm"},
    "knowledgebase_settings": {"knowledgebases": []},
    "conversation_settings": {"commands": []},
    "mcp_server_config": {"mcpServers": {}},
    "role_prompts": [],
}


class _StubRM(BaseResourceManager):
    """覆盖 ``retrieve_agent_config`` 以避开 HTTP，用于验证 security_settings 下发通道。"""

    def __init__(self, security_settings: dict | None):
        super().__init__(app_code="x", app_secret="y")
        self._security_settings = security_settings

    def retrieve_agent_config(self, agent_code: str, version=None, **kwargs) -> dict:
        raw = dict(_BASE_RAW)
        if self._security_settings is not None:
            raw["security_settings"] = self._security_settings
        return raw


class TestAgentConfigSecuritySettingsChannel:
    """`get_agent_config` 是从平台下发构造 SecuritySettings 的唯一入口。"""

    @pytest.mark.parametrize(
        "platform, expected_blacklist",
        [
            ({"command_blacklist": False}, False),  # 平台下发覆盖默认
            ({"command_blacklist": True}, True),
            (None, True),  # 无 security_settings 字段 → 字段默认
        ],
    )
    def test_platform_values_flow_into_agent_config(self, platform, expected_blacklist):
        rm = _StubRM(platform)
        cfg = rm.get_agent_config("a1")
        assert isinstance(cfg.security_settings, SecuritySettings)
        assert cfg.security_settings.command_blacklist is expected_blacklist

    def test_from_mapping_is_construction_point(self):
        """SecuritySettings 由 from_mapping 构造，AgentConfig 上可直接读到。"""
        rm = _StubRM({"content_safety": False})
        cfg = rm.get_agent_config("a1")
        assert cfg.security_settings.content_safety is False


class TestMandatoryRedactionNotDisableable:
    """mandatory 三项脱敏被平台下发 False 时静默归 True（D-06 / SEC-19）。"""

    @pytest.mark.parametrize(
        "field",
        ["redact_registered_secrets", "redact_authorization_headers", "redact_private_keys"],
    )
    def test_platform_false_forced_true(self, field):
        settings = SecuritySettings.from_mapping({field: False})  # 不抛异常
        assert getattr(settings, field) is True

    def test_direct_construction_also_protected(self):
        """直接构造路径同样受保护（防绕过 from_mapping）。"""
        settings = SecuritySettings(redact_registered_secrets=False)
        assert settings.redact_registered_secrets is True

    def test_does_not_raise(self):
        SecuritySettings.from_mapping({field: False for field in (
            "redact_registered_secrets", "redact_authorization_headers", "redact_private_keys"
        )})
        assert True  # 未抛 ValidationError


class TestOptionalRedactionDisableable:
    """optional 五项脱敏可被逐项关闭。"""

    @pytest.mark.parametrize(
        "field",
        [
            "redact_vendor_tokens",
            "redact_structured_fields",
            "redact_url_credentials",
            "redact_dsn_passwords",
            "redact_cookies",
        ],
    )
    def test_platform_false_respected(self, field):
        settings = SecuritySettings.from_mapping({field: False})
        assert getattr(settings, field) is False


class TestBareEntropyConfig:
    """裸熵兜底三配置项：默认值 + 平台覆盖 + 非 mandatory（CONTEXT D-07 / DESIGN §3.6）。"""

    @pytest.mark.parametrize(
        "mapping, expected",
        [
            (None, True),  # 默认开启（全 purpose，默认真实生效）
            ({"redact_bare_entropy": False}, False),  # heuristic 层可关
            ({"redact_bare_entropy": True}, True),
        ],
    )
    def test_enabled_switch(self, mapping, expected):
        settings = SecuritySettings.from_mapping(mapping) if mapping is not None else SecuritySettings()
        assert settings.redact_bare_entropy is expected

    @pytest.mark.parametrize(
        "mapping, expected",
        [
            (None, 32),  # 默认最小长度 32
            ({"redact_secrets_min_length": 16}, 16),  # 平台下发可放宽
        ],
    )
    def test_min_length(self, mapping, expected):
        settings = SecuritySettings.from_mapping(mapping) if mapping is not None else SecuritySettings()
        assert settings.redact_secrets_min_length == expected

    @pytest.mark.parametrize(
        "mapping, expected",
        [
            (None, 4.2),  # 默认 alnum/Base64URL 阈值
            ({"redact_secrets_entropy_threshold": 5.0}, 5.0),
        ],
    )
    def test_entropy_threshold(self, mapping, expected):
        settings = SecuritySettings.from_mapping(mapping) if mapping is not None else SecuritySettings()
        assert settings.redact_secrets_entropy_threshold == expected

    def test_not_mandatory(self):
        """裸熵是 heuristic 层，按 design 可关 —— 不得进入 mandatory 集。"""
        assert "redact_bare_entropy" not in _MANDATORY_REDACT_FIELDS
