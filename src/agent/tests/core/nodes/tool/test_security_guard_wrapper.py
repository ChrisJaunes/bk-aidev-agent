# -*- coding: utf-8 -*-
"""Tests for the tool-result security guard wrapper (aidev_agent.core.nodes.tool.security_wrapper)."""

from __future__ import annotations

from types import SimpleNamespace

from aidev_agent.core.nodes.tool.security_wrapper import build_security_guard_sync_wrapper
from aidev_agent.packages.security.threat_patterns import UNTRUSTED_START
from langchain_core.messages import ToolMessage


def _make_request(name: str, tool=None) -> SimpleNamespace:
    return SimpleNamespace(tool_call={"name": name}, tool=tool, state={})


def _run_wrapper(request, content, name: str, known_values=()) -> ToolMessage:
    from aidev_agent.pydantic_models import SecuritySettings

    settings = SecuritySettings(known_sensitive_values=",".join(known_values)) if known_values else None
    wrapper = build_security_guard_sync_wrapper(settings=settings)
    msg = ToolMessage(content=content, tool_call_id="id-1", name=name)
    result = wrapper(request, lambda _req: msg)
    assert isinstance(result, ToolMessage)
    return result


class TestSecurityGuardWrapper:
    """工具结果安全防护：脱敏 + 不可信包裹。"""

    def test_redacts_secrets_in_trusted_tool_result(self):
        secret = "sk-" + "a" * 36
        msg = _run_wrapper(_make_request("knowledge_retrieval"), f"token is {secret}", "knowledge_retrieval")
        assert secret not in msg.content
        # MODEL_OUTPUT purpose 下掩码为 typed sentinel，故断言 sentinel 前缀；
        # 不绑定具体 kind —— kind 命名后续可能调整（CONTEXT D-11）。
        assert "[REDACTED:" in msg.content

    def test_redacts_credential_key_value(self):
        msg = _run_wrapper(_make_request("ask_user_question"), "password=supersecret", "ask_user_question")
        assert "supersecret" not in msg.content
        # 同上：只绑 sentinel 前缀，不绑具体 kind（CONTEXT D-11）。
        assert "[REDACTED:" in msg.content

    def test_wraps_untrusted_tool_result(self):
        msg = _run_wrapper(_make_request("web_search"), "external page content", "web_search")
        assert msg.content.startswith(UNTRUSTED_START)

    def test_trusted_tool_result_not_wrapped(self):
        msg = _run_wrapper(_make_request("knowledge_retrieval"), "plain text", "knowledge_retrieval")
        assert UNTRUSTED_START not in msg.content

    def test_redacts_untrusted_tool_result_before_wrapping(self):
        secret = "ghp_" + "A" * 36
        msg = _run_wrapper(_make_request("mcp_get_file"), f"creds {secret}", "mcp_get_file")
        assert secret not in msg.content
        assert msg.content.startswith(UNTRUSTED_START)

    def test_wraps_mcp_tool_via_metadata(self):
        # MCP 工具名不带前缀，靠 request.tool.metadata.mcp_name 判定
        mcp_tool = SimpleNamespace(metadata={"mcp_name": "weather_server"})
        msg = _run_wrapper(_make_request("get_weather", tool=mcp_tool), "sunny", "get_weather")
        assert msg.content.startswith(UNTRUSTED_START)

    def test_sanitizes_pollution_keys_in_mcp_result(self):
        mcp_tool = SimpleNamespace(metadata={"mcp_name": "evil_server"})
        # MCP 工具返回结构化内容块列表（langchain_mcp_adapters 的 CallToolResult 形态）
        payload = [
            {"type": "text", "text": "hello", "__proto__": {"polluted": True}},
            {"type": "text", "text": "world", "constructor": "evil"},
        ]
        msg = _run_wrapper(_make_request("query", tool=mcp_tool), payload, "query")
        # 危险键被剔除，正常内容保留，且结果被包裹
        assert "__proto__" not in msg.content
        assert "constructor" not in msg.content
        assert "hello" in msg.content
        assert "world" in msg.content
        assert msg.content.startswith(UNTRUSTED_START)


class TestKnownValuesInjection:
    """已知敏感值经 ``SecuritySettings.known_sensitive_values`` 送入 detector 管线。

    此前 ``scan_text`` 的 ``known_values`` 参数无任何生产调用点，
    ``RegisteredSecretDetector`` 恒为空；现统一由 ``settings`` 单通道下发。
    """

    SECRET = "SUPER-SECRET-VALUE-12345"

    def test_known_value_redacted_in_str_content(self):
        """str 形态：已知值被 detector 管线命中并掩码。"""
        msg = _run_wrapper(
            _make_request("knowledge_retrieval"),
            f"config holds {self.SECRET} here",
            "knowledge_retrieval",
            known_values=[self.SECRET],
        )
        assert self.SECRET not in msg.content
        assert "[REDACTED:" in msg.content

    def test_known_value_redacted_in_structured_content(self):
        """结构化形态：dict/list 同样享受已知值脱敏（覆盖缺口回归）。

        ``redact_payload`` 若不把 ``settings`` 递归透传，此用例会失败。
        """
        payload = [{"type": "text", "text": f"here is {self.SECRET}"}]
        msg = _run_wrapper(
            _make_request("knowledge_retrieval"),
            payload,
            "knowledge_retrieval",
            known_values=[self.SECRET],
        )
        assert self.SECRET not in str(msg.content)
        assert "[REDACTED:" in str(msg.content)

    def test_no_known_values_leaves_value_intact(self):
        """未注入时已知值不脱敏 —— 证明生效源于注入而非既有模式规则。"""
        msg = _run_wrapper(
            _make_request("knowledge_retrieval"),
            f"config holds {self.SECRET} here",
            "knowledge_retrieval",
        )
        assert self.SECRET in msg.content

    def test_same_mechanism_yields_distinct_placeholder_per_outlet(self):
        """两条出口共享 detector 匹配机制，但各自保留自己的占位符文案。

        detector 管线（模型上下文）用 ``[REDACTED:*]``；工具返回值出口用
        legacy ``__BKAI_AGENT_REDACTED__``（用户可见文案契约）。
        两个占位符服务不同出口，不可被未来重构统一（见 operations 模块 docstring）。
        """
        from aidev_agent.packages.security.redaction import (
            KNOWN_VALUES_PLACEHOLDER,
            redact_known_values,
        )

        text = f"holds {self.SECRET} here"
        detector_output = _run_wrapper(
            _make_request("knowledge_retrieval"),
            text,
            "knowledge_retrieval",
            known_values=[self.SECRET],
        ).content
        legacy_output = redact_known_values(text, [self.SECRET])

        assert "[REDACTED:" in detector_output
        assert KNOWN_VALUES_PLACEHOLDER in legacy_output
        assert "[REDACTED:" not in legacy_output
        # 两者互不污染：detector 出口绝不用 legacy 文案
        assert KNOWN_VALUES_PLACEHOLDER not in detector_output

        # 匹配机制一致：两者对同一文本命中了同一段（位置等价）。
        # 用正则归一化 sentinel 文案，不绑定具体 kind（CONTEXT D-11）。
        import re

        normalized = re.sub(r"\[REDACTED:[^\]]*\]", "X", detector_output)
        assert normalized == legacy_output.replace(KNOWN_VALUES_PLACEHOLDER, "X")
