# -*- coding: utf-8 -*-
"""Tests for command approval (aidev_agent.packages.security.command.command_approval)."""

from __future__ import annotations

import aidev_agent.packages.security.command.command_approval as ca
from aidev_agent.pydantic_models import SecuritySettings


def _ss(approvers: str = "", enabled: bool = True) -> SecuritySettings:
    """构造显式安全配置（command_approval 配置由调用方传入，不再读 env）。"""
    return SecuritySettings(command_approval=enabled, command_approval_approvers=approvers)


class TestCommandFingerprint:
    def test_normalizes_whitespace_and_case(self):
        assert ca.command_fingerprint("  Echo   Hello  ") == ca.command_fingerprint("echo hello")

    def test_different_commands_diverge(self):
        assert ca.command_fingerprint("echo a") != ca.command_fingerprint("echo b")


class TestRequireCommandApproval:
    def test_rejects_non_string(self):
        assert ca.require_command_approval(None, security_settings=_ss("u1")) is False
        assert ca.require_command_approval(123, security_settings=_ss("u1")) is False

    def test_rejects_when_no_approvers(self):
        assert ca.require_command_approval("echo hi", security_settings=_ss("")) is False

    def test_approved_decision_returns_true(self, monkeypatch):
        monkeypatch.setattr(ca, "interrupt", lambda value: {"payload": {"approved": True}})
        assert ca.require_command_approval("echo hi", target_runtime="sbx", security_settings=_ss("u1,u2")) is True

    def test_rejected_decision_returns_false(self, monkeypatch):
        monkeypatch.setattr(ca, "interrupt", lambda value: {"payload": {"approved": False}})
        assert ca.require_command_approval("echo hi", security_settings=_ss("u1")) is False

    def test_interrupt_exception_fails_closed(self, monkeypatch):
        def _boom(_value):
            raise RuntimeError("no graph context")

        monkeypatch.setattr(ca, "interrupt", _boom)
        assert ca.require_command_approval("echo hi", security_settings=_ss("u1")) is False


class TestGetCommandApprovalMode:
    def test_defaults_to_manual(self):
        assert ca.get_command_approval_mode(_ss()) == "manual"

    def test_unknown_value_falls_back_to_manual(self):
        assert ca.get_command_approval_mode(SecuritySettings(command_approval_mode="bogus")) == "manual"
