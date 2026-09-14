# -*- coding: utf-8 -*-
"""Tests for command risk assessor (smart approval mode).

Covers the fail-closed contract of ``CommandRiskAssessor.assess``:
non-string commands, missing LLM, LLM exceptions and invalid outputs all
must fall back to ``uncertain`` (never auto-approve).
"""

from __future__ import annotations

from typing import Any

from aidev_agent.packages.security.command.command_risk_assessor import CommandRiskAssessor, RiskAssessment


class _StructuredLLM:
    """Fake LLM that supports ``with_structured_output``."""

    def __init__(self, risk: Any = "low", reason: str = "") -> None:
        self._risk = risk
        self._reason = reason

    def with_structured_output(self, schema: Any) -> "_StructuredLLM":
        return self

    def invoke(self, prompt: str) -> RiskAssessment:
        return RiskAssessment(risk=self._risk, reason=self._reason)  # type: ignore[arg-type]


class _TextOnlyLLM:
    """Fake LLM without structured output; returns raw JSON text."""

    def __init__(self, content: str) -> None:
        self._content = content

    def with_structured_output(self, schema: Any) -> Any:
        raise AttributeError("structured output unsupported")

    def invoke(self, prompt: str) -> Any:
        return type("Message", (), {"content": self._content})()


class _BoomLLM:
    """Fake LLM that raises on every call."""

    def with_structured_output(self, schema: Any) -> Any:
        raise RuntimeError("boom")


class TestCommandRiskAssessor:
    def test_non_string_command_returns_uncertain(self):
        assessor = CommandRiskAssessor(_StructuredLLM("low"))
        assert assessor.assess(None) == "uncertain"
        assert assessor.assess(123) == "uncertain"
        assert assessor.assess("   ") == "uncertain"

    def test_missing_llm_returns_uncertain(self):
        assessor = CommandRiskAssessor(None)
        assert assessor.assess("rm -rf /") == "uncertain"

    def test_low_risk(self):
        assessor = CommandRiskAssessor(_StructuredLLM("low"))
        assert assessor.assess("ls -la") == "low"

    def test_high_risk(self):
        assessor = CommandRiskAssessor(_StructuredLLM("high"))
        assert assessor.assess("rm -rf /") == "high"

    def test_uncertain_risk(self):
        assessor = CommandRiskAssessor(_StructuredLLM("uncertain"))
        assert assessor.assess("some-unknown-tool") == "uncertain"

    def test_invalid_risk_falls_back_to_uncertain(self):
        # 文本 LLM 返回非法 risk 值 → assess 防御逻辑回退 uncertain
        assessor = CommandRiskAssessor(_TextOnlyLLM('{"risk": "bogus", "reason": "x"}'))
        assert assessor.assess("ls") == "uncertain"

    def test_llm_exception_falls_back_to_uncertain(self):
        assessor = CommandRiskAssessor(_BoomLLM())
        assert assessor.assess("ls") == "uncertain"

    def test_text_llm_json_fallback(self):
        assessor = CommandRiskAssessor(_TextOnlyLLM('{"risk": "low", "reason": "safe"}'))
        assert assessor.assess("ls") == "low"

    def test_text_llm_invalid_json_falls_back_to_uncertain(self):
        assessor = CommandRiskAssessor(_TextOnlyLLM("not json"))
        assert assessor.assess("ls") == "uncertain"
