# -*- coding: utf-8 -*-
"""命令安全编排（三层防护）下沉后语义测试。

覆盖 ``packages.security.command.command_security`` 的：
- ``ensure_non_empty`` 空值兜底与 ``ENSURE_NON_EMPTY_HINT`` 常量；
- ``enforce_command_security`` 黑名单 / 白名单 / 审批三条主路径与 smart 风险评估分支。

本文件刻意**不 import** ``core.tools.runtime_tools`` 的任何类型，以证明
``packages/security/`` 自足、不反向依赖 core。
"""

from __future__ import annotations

import pytest
from aidev_agent.packages.security.command import command_security as cs
from aidev_agent.packages.security.command.command_security import (
    ENSURE_NON_EMPTY_HINT,
    enforce_command_security,
    ensure_non_empty,
)
from aidev_agent.packages.security.redaction.operations import (
    KNOWN_VALUES_PLACEHOLDER,
    redact_known_values,
)
from aidev_agent.pydantic_models import SecuritySettings


class TestEnsureNonEmpty:
    @pytest.mark.parametrize(
        "value, expected",
        [("", ENSURE_NON_EMPTY_HINT), ("   ", ENSURE_NON_EMPTY_HINT), ("ok", "ok")],
    )
    def test_ensure_non_empty(self, value, expected):
        """空 / 纯空白 -> 提示；非空 -> 原样返回。"""
        assert ensure_non_empty(value) == expected

    def test_hint_prefix(self):
        """提示常量保留 [harness] 前缀（与 core 侧文案一致）。"""
        assert ENSURE_NON_EMPTY_HINT.startswith("[harness]")


class TestKnownValuesDelegation:
    """T-urf-04：redaction.operations 的替换语义（core 薄委托的单一真源）。"""

    @pytest.mark.parametrize(
        "text, values, expected",
        [
            ("token is abc123", ["abc123"], "token is __BKAI_AGENT_REDACTED__"),
            ("user=admin token=xyz", ["admin", "xyz"], "user=__BKAI_AGENT_REDACTED__ token=__BKAI_AGENT_REDACTED__"),
            ("no secrets here", ["secret_token"], "no secrets here"),
            ("some text", [], "some text"),
            ("some text", [""], "some text"),
            ("key=abc key=abc", ["abc"], "key=__BKAI_AGENT_REDACTED__ key=__BKAI_AGENT_REDACTED__"),
        ],
    )
    def test_redact_known_values_equivalence(self, text, values, expected):
        """与 core 侧 ``test_security.py::TestRedactOutput`` 同输入同断言 —— 钉住委托等价性。"""
        assert redact_known_values(text, values) == expected

    def test_placeholder_is_legacy_string(self):
        """legacy 占位符由 redaction.operations 提供，用户可见文案契约不可改。"""
        assert KNOWN_VALUES_PLACEHOLDER == "__BKAI_AGENT_REDACTED__"


class TestEnforceCommandSecurity:
    @pytest.mark.parametrize(
        "settings_kwargs, command, match",
        [
            # 未注入配置：白名单命令放行
            (None, "ls /tmp", None),
            # 未注入配置：灰名单命令回落白名单拒绝（无黑名单字样）
            (None, "rm -rf /", "命令执行被拒绝"),
            # 黑名单开启：命中危险命令
            ({"command_blacklist": True}, "rm -rf /", "危险命令黑名单"),
            # 黑名单关闭：同命令回落白名单拒绝
            ({"command_blacklist": False}, "rm -rf /", "命令执行被拒绝"),
            # 黑名单关闭 + 白名单命令：放行
            ({"command_blacklist": False}, "echo hi", None),
            # 审批未启用：灰名单命令维持白名单拒绝
            ({"command_approval": False}, "touch /tmp/x", "不在允许列表中"),
        ],
    )
    def test_layer_paths(self, settings_kwargs, command, match):
        """黑名单 / 白名单 / 审批三层主路径（None 表示放行不抛）。"""
        ss = SecuritySettings(**settings_kwargs) if settings_kwargs is not None else None
        if match is None:
            enforce_command_security(command, "local", ss)
        else:
            with pytest.raises(ValueError, match=match):
                enforce_command_security(command, "local", ss)

    def test_blacklist_rejection_excludes_whitelist_wording(self):
        """黑名单关闭时灰名单命令的拒绝理由不含「黑名单」字样。"""
        ss = SecuritySettings(command_blacklist=False)
        with pytest.raises(ValueError) as exc:
            enforce_command_security("rm -rf /", "local", ss)
        assert "黑名单" not in str(exc.value)


class TestApprovalBranch:
    """Layer 2 审批分支（patch 本模块内的 require_command_approval 名字）。"""

    @staticmethod
    def _approval_settings() -> SecuritySettings:
        return SecuritySettings(command_approval=True, command_approval_approvers="u1")

    @pytest.mark.parametrize("approved, should_raise", [(False, True), (True, False)])
    def test_manual_approval_outcome(self, monkeypatch, approved, should_raise):
        """审批拒绝 -> 抛「命令审批未通过」；通过 -> 放行。"""
        monkeypatch.setattr(cs, "require_command_approval", lambda *a, **kw: approved)
        if should_raise:
            with pytest.raises(ValueError, match="命令审批未通过"):
                enforce_command_security("touch /tmp/x", "local", self._approval_settings())
        else:
            enforce_command_security("touch /tmp/x", "local", self._approval_settings())

    def test_smart_high_risk_rejected(self):
        """smart 模式 + high 风险 -> 直接拒绝（不进入 ITSM）。"""
        ss = SecuritySettings(command_approval=True, command_approval_mode="smart", command_approval_approvers="u1")
        assessor = type("R", (), {"assess": lambda self, c: "high"})()
        with pytest.raises(ValueError, match="风险评估：high"):
            enforce_command_security("touch /tmp/x", "local", ss, assessor)
