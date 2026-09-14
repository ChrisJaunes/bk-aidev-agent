# -*- coding: utf-8 -*-
"""Tests for the redaction operations layer (aidev_agent.packages.security.redaction.operations)."""

from __future__ import annotations

import pathlib

import pytest
from aidev_agent.packages.security.redaction.operations import (
    KNOWN_VALUES_PLACEHOLDER,
    count_sensitive_hits,
    redact_for_export,
    redact_known_values,
    redact_payload,
    redact_text,
    scan_text,
)
from aidev_agent.packages.security.redaction.policy import RedactionPurpose

_GPG_SAMPLE = "QGBZvSrgJ0hZOs9KHJV4jhw4hKFPGI6G"  # len=32，GPG key 样值


class TestRedactTextGpgKey:
    """起点缺陷 D1：GPG_KEY=<32字符样值> 在三种 purpose 下均被替换。"""

    @pytest.mark.parametrize(
        "purpose",
        [RedactionPurpose.LOG, RedactionPurpose.MODEL_OUTPUT, RedactionPurpose.EXPORT],
    )
    def test_gpg_key_redacted_all_purposes(self, purpose):
        out = redact_text(f"GPG_KEY={_GPG_SAMPLE}", purpose=purpose)
        assert _GPG_SAMPLE not in out

    @pytest.mark.parametrize(
        "value",
        ["0123456789abcdef", "0123456789abcdef0123456789abcdef01234567"],
    )
    def test_gpg_hex_identifiers_untouched(self, value):
        """16 位 key ID / 40 位 fingerprint 是纯 hex 定长串，不误伤。"""
        out = redact_text(f"GPG_KEY={value}", purpose=RedactionPurpose.LOG)
        assert value in out


class TestRedactTextVendor:
    """厂商 token 按 purpose 选择掩码风格。"""

    def test_partial_under_log(self):
        assert (
            redact_text("prefix sk-" + "a" * 36 + " suffix", purpose=RedactionPurpose.LOG)
            == "prefix sk-aaa...aaaa suffix"
        )

    def test_typed_sentinel_under_model_output(self):
        out = redact_text("prefix sk-" + "a" * 36 + " suffix", purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out == "prefix [REDACTED:openai_key] suffix"


class TestRedactTextIdempotence:
    """幂等：redact(redact(x)) == redact(x)；未命中区间字节级不变。"""

    @pytest.mark.parametrize("purpose", [RedactionPurpose.LOG, RedactionPurpose.MODEL_OUTPUT, RedactionPurpose.EXPORT])
    def test_idempotent(self, purpose):
        text = "token sk-" + "a" * 36 + " and GPG_KEY=" + _GPG_SAMPLE
        once = redact_text(text, purpose=purpose)
        assert redact_text(once, purpose=purpose) == once

    def test_unmatched_segments_preserved(self):
        text = "plain prefix " + "sk-" + "a" * 36 + " plain suffix"
        out = redact_text(text, purpose=RedactionPurpose.LOG)
        assert out.startswith("plain prefix ")
        assert out.endswith(" plain suffix")


class TestRedactPayload:
    """D6 修复：凭据容器递归进入，不整体丢弃。"""

    def test_credentials_container_recursed(self):
        payload = {"credentials": {"user": "a", "password": "b"}}
        out = redact_payload(payload, purpose=RedactionPurpose.LOG)
        assert isinstance(out["credentials"], dict)
        assert out["credentials"]["user"] == "a"
        assert out["credentials"]["password"] != "b"

    def test_top_level_credential_field_replaced(self):
        out = redact_payload({"api_key": "somevalue"}, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out["api_key"] == "[REDACTED:credential]"

    def test_nested_list_recursed(self):
        out = redact_payload({"items": [{"password": "x"}]}, purpose=RedactionPurpose.LOG)
        assert out["items"][0]["password"] != "x"


class TestScanText:
    """ScanResult 契约：raw_rule_hits 键为 rule_id。"""

    def test_raw_rule_hits_keyed_by_rule_id(self):
        result = scan_text("prefix sk-" + "a" * 36 + " suffix", purpose=RedactionPurpose.LOG)
        assert "vendor.openai" in result.raw_rule_hits
        assert result.unique_findings >= 1

    def test_empty_text_returns_zero(self):
        result = scan_text("", purpose=RedactionPurpose.LOG)
        assert result.unique_findings == 0
        assert result.raw_rule_hits == {}


class TestLegacyCompat:
    """兼容旧调用口径。"""

    def test_count_sensitive_hits_keys(self):
        hits = count_sensitive_hits("key sk-" + "a" * 36)
        assert set(hits) == {"vendor_token", "credential_key_value", "total"}

    def test_redact_for_export_uses_export_purpose(self):
        payload = {"secret": "abc"}
        assert redact_for_export(payload)["secret"] != "abc"


class TestRedactKnownValues:
    """已知值精确替换节（原 known_values 模块，合入 operations）。"""

    @pytest.mark.parametrize(
        ("text", "values", "expected"),
        [
            ("token is abc123", ["abc123"], f"token is {KNOWN_VALUES_PLACEHOLDER}"),
            (
                "user=admin token=xyz",
                ["admin", "xyz"],
                f"user={KNOWN_VALUES_PLACEHOLDER} token={KNOWN_VALUES_PLACEHOLDER}",
            ),
            ("no hit here", ["absent"], "no hit here"),
            ("plain text", [], "plain text"),
            ("empty value stays", [""], "empty value stays"),
            ("key=abc key=abc", ["abc"], f"key={KNOWN_VALUES_PLACEHOLDER} key={KNOWN_VALUES_PLACEHOLDER}"),
        ],
    )
    def test_replace_semantics(self, text, values, expected):
        """与迁移前 command_security.redact_output 的替换语义逐例等价。"""
        assert redact_known_values(text, values) == expected

    def test_legacy_placeholder_is_user_visible_contract(self):
        """legacy 占位符文案是用户可见契约，不可与 masking.REDACT_PLACEHOLDER 统一。"""
        from aidev_agent.packages.security.redaction.masking import REDACT_PLACEHOLDER

        assert KNOWN_VALUES_PLACEHOLDER == "__BKAI_AGENT_REDACTED__"
        assert KNOWN_VALUES_PLACEHOLDER != REDACT_PLACEHOLDER

    def test_regex_metacharacters_are_literal(self):
        """精确替换不走正则 —— 元字符按字面匹配，不产生误伤。"""
        assert redact_known_values("a.b.c", ["."]) == f"a{KNOWN_VALUES_PLACEHOLDER}b{KNOWN_VALUES_PLACEHOLDER}c"

    @pytest.mark.parametrize("values", [["abc", "abcdef"], ["abcdef", "abc"]])
    def test_longer_value_wins_regardless_of_input_order(self, values):
        """复用 detector 的核心收益：长值优先命中，短值不再把长值切碎。

        旧的逐值 ``str.replace`` 会让 ``abc`` 先命中，把 ``abcdef`` 打成残段，
        且结果依赖入参顺序。
        """
        assert redact_known_values("abcdef", values) == KNOWN_VALUES_PLACEHOLDER

    def test_overlapping_values_produce_single_placeholder(self):
        """重叠 span 经 ``merge_spans`` 合并 —— 不产生拼接残段。"""
        assert redact_known_values("xabcdefy", ["abc", "abcdef"]) == f"x{KNOWN_VALUES_PLACEHOLDER}y"

    def test_uses_same_matching_mechanism_as_detector_pipeline(self):
        """匹配机制与 ``scan_text`` 的 detector 管线一致（同 detector spans）。"""
        from aidev_agent.packages.security.redaction.detectors import RegisteredSecretDetector
        from aidev_agent.packages.security.redaction.findings import merge_spans

        values = ["tok_abc123", "abc"]
        text = "creds tok_abc123 and abc"
        spans = [(f.start, f.end) for f in merge_spans(RegisteredSecretDetector.from_values(values).scan(text))]

        # 逐个 span 独立替换后应得到同样的结果
        expected = text
        for start, end in sorted(spans, reverse=True):
            expected = expected[:start] + KNOWN_VALUES_PLACEHOLDER + expected[end:]
        assert redact_known_values(text, values) == expected

    def test_does_not_import_config_or_backend(self):
        """已知值节仍不得 import 配置 / backend（依赖方向约束保留）。

        对齐 detector 后允许引用同包 ``detectors`` / ``findings``，
        但**配置与 backend 的解析必须留在调用方（core）**。

        用 AST 而非文本搜索判定 —— 注释里写「禁止 import aidev_agent.config」
        不应被误判为违规。
        """
        import ast

        src = pathlib.Path("aidev_agent/packages/security/redaction/operations.py").read_text(encoding="utf-8")
        section = "# ===== 已知值精确替换" + src.split("# ===== 已知值精确替换")[1]

        imported: set[str] = set()
        for node in ast.walk(ast.parse(section)):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)

        forbidden = ("aidev_agent.config", "aidev_agent.core", "aidev_agent.services", "aidev_agent.api")
        offenders = [mod for mod in imported if mod.startswith(forbidden)]
        assert not offenders, f"已知值节不应 import {offenders}"
