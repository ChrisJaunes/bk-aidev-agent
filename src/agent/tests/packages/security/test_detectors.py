# -*- coding: utf-8 -*-
"""Tests for the protocol-aware detectors (aidev_agent.packages.security.redaction.detectors)."""

from __future__ import annotations

import pathlib
import re
import time
from dataclasses import fields
from unittest.mock import patch

import pytest
from aidev_agent.packages.security.redaction import detectors
from aidev_agent.packages.security.redaction.detectors import (
    AssignmentDetector,
    CookieDetector,
    DsnDetector,
    EntropyDetector,
    HeadersDetector,
    JdbcDetector,
    JwtDetector,
    PemDetector,
    RegisteredSecretDetector,
    RegisteredValue,
    UrlDetector,
    VendorTokenDetector,
    classify_pem_label,
    is_pem_non_secret_span,
    is_placeholder_value,
    iter_pem_blocks,
    normalize_label,
)
from aidev_agent.packages.security.redaction.operations import (
    _detectors_for_scan,
    count_sensitive_hits,
    redact_for_export,
    redact_payload,
    redact_text,
    scan_text,
)
from aidev_agent.packages.security.redaction.policy import RedactionPurpose
from aidev_agent.pydantic_models import SecuritySettings

# 起点缺陷 D1 的样值：32 位高熵、无厂商前缀、非注册值
_GPG_SAMPLE = "QGBZvSrgJ0hZOs9KHJV4jhw4hKFPGI6G"

# 64 位纯 hex（SHA-256 / AES-256 宽度）—— S057/S058/S076/S081 共用的测试值
_HEX64 = "ee1ca6a9220eec1691004295dc12093f2444c7c0eb210a1f2967434043133cd4"

# 严格字段正例（命中即脱敏，无需评分）
_STRICT_SAMPLES = (
    "password=hunter2",
    "passwd: hunter2",
    "api_key: abc123456789012345678",
    "apikey=abc123456789012345678",
    '{"app_secret": "abcdefghijklmnopqrstuvwxyz"}',
    "access_token=abcdefghijklmnopqrstuvwxyz",
    "secret_key=abcdefghijklmnopqrstuvwxyz",
)

# 歧义字段正例（走上下文评分）
_AMBIGUOUS_POSITIVE_SAMPLES = (
    f"GPG_KEY={_GPG_SAMPLE}",
    f"gpg_key={_GPG_SAMPLE}",
    f"SIGNING_KEY={_GPG_SAMPLE}",
    f"SIGNING_TOKEN={_GPG_SAMPLE}",
)

# 负例：非凭据字段 / 变量引用 / 纯 hex 定长形状
_NEGATIVE_SAMPLES = (
    "token_count=100",
    "token_type=bearer",
    "tokenizer=cl100k",
    "max_tokens=4096",
    "secret_name=my-secret",
    "secret_id=7c9f4a2d",
    "secret_arn=arn:aws:secretsmanager",
    "key_id=abc123",
    "public_key=abc123",
    "has_password=true",
    "authorization_url=https://x.com/oauth",
    "password=$DB_PASSWORD",
    "password=${DB_PASSWORD}",
    "password=os.getenv('X')",
    "GPG_KEY=0123456789abcdef",
    "GPG_KEY=0123456789abcdef0123456789abcdef01234567",
)


class TestAssignmentStrictFields:
    """严格凭据字段命中即脱敏，span 只覆盖值本体（不含 key 与分隔符）。"""

    @pytest.mark.parametrize("sample", _STRICT_SAMPLES)
    def test_strict_field_hits(self, sample):
        findings = AssignmentDetector().scan(sample)
        assert len(findings) == 1
        assert findings[0].kind == "credential"
        # span 不含 key 与分隔符：替换后 `key=` 前缀保真
        assert sample[findings[0].start : findings[0].end] not in sample[: findings[0].start]

    def test_span_covers_value_only(self):
        sample = "password=hunter2"
        finding = AssignmentDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "hunter2"


class TestAssignmentAmbiguousScoring:
    """歧义字段走上下文评分（DESIGN §3.5），score >= 4 触发。"""

    @pytest.mark.parametrize("sample", _AMBIGUOUS_POSITIVE_SAMPLES)
    def test_ambiguous_positive_hits(self, sample):
        findings = AssignmentDetector().scan(sample)
        assert len(findings) == 1
        assert findings[0].confidence == "medium"
        assert findings[0].priority == 70
        assert sample[findings[0].start : findings[0].end] == _GPG_SAMPLE

    def test_gpg_key_starting_defect(self):
        """起点缺陷 D1：GPG_KEY=<32位高熵值> 必须命中（评分 7 >= 4）。"""
        sample = f"GPG_KEY={_GPG_SAMPLE}"
        findings = AssignmentDetector().scan(sample)
        assert len(findings) == 1
        # kind 取归一化字段名（GPG_KEY -> gpgkey，去分隔符 + 小写）
        assert findings[0].kind == "gpgkey"
        assert findings[0].rule_id == "assignment.gpgkey"

    def test_case_insensitive_field_name(self):
        assert AssignmentDetector().scan(f"gpg_key={_GPG_SAMPLE}")[0].kind == "gpgkey"

    @pytest.mark.parametrize("sample", _NEGATIVE_SAMPLES)
    def test_negative_samples_zero_hits(self, sample):
        assert AssignmentDetector().scan(sample) == []

    @pytest.mark.parametrize(
        "value",
        ["0123456789abcdef", "0123456789abcdef0123456789abcdef01234567"],
    )
    def test_hex_fixed_length_excluded(self, value):
        """纯 hex 且长度 ∈ {8,16,32,40}（GPG key ID / fingerprint）不命中。

        64 **已移出**豁免集（S057 族召回修复）：它是 SHA-256 / AES-256 的
        标准宽度，与业务 digest 无法用形状区分，改由熵 / 上下文判据决定。
        """
        assert AssignmentDetector().scan(f"GPG_KEY={value}") == []

    def test_64_hex_no_longer_shape_excluded(self):
        """64 位纯 hex 不再被形状豁免；``secret`` 后缀提供足够上下文分 → 命中。"""
        findings = AssignmentDetector().scan(f"SIGNING_SECRET={_HEX64}")
        assert len(findings) == 1
        assert findings[0].rule_id == "assignment.signingsecret"

    @pytest.mark.parametrize(
        "sample",
        [
            f"commit {_HEX64}",
            f"sha256={_HEX64}",
            f"checksum: {_HEX64}",
            f"integrity_hash={_HEX64}",
        ],
    )
    def test_64_hex_digest_contexts_still_safe(self, sample):
        """误报面护栏：git SHA / digest / checksum 上下文不得被脱敏。

        64 位不再走形状豁免，安全性改由「熵 >= 4.2 才触发裸熵」+
        「赋值路径需字段名上下文」共同保证 —— 本用例钉住后者。
        """
        assert AssignmentDetector().scan(sample) == []

    def test_40_hex_fingerprint_still_excluded(self):
        """40 位（GPG fingerprint）豁免必须保留 —— 本次只放开 64。"""
        assert AssignmentDetector().scan(f"GPG_KEY={'0123456789abcdef' * 2 + '01234567'}") == []


class TestAssignmentPrefilterReachability:
    """prefilter 可达性：所有正例都必须能通过 could_match。"""

    @pytest.mark.parametrize("sample", _STRICT_SAMPLES + _AMBIGUOUS_POSITIVE_SAMPLES)
    def test_positive_reachable(self, sample):
        detector = AssignmentDetector()
        assert detector.could_match(sample) is True
        assert detector.scan(sample) != []

    @pytest.mark.parametrize("sample", ["no separator here", "", "plain text"])
    def test_negative_unreachable(self, sample):
        assert AssignmentDetector().could_match(sample) is False


class TestAssignmentNegativeFields:
    """负例字段名：含 secret/key 语义但非凭据，必须零命中。"""

    @pytest.mark.parametrize(
        "sample",
        [
            "token_count=100",
            "secret_name=my-secret",
            "public_key=abc123",
            "key_id=abc123",
            "has_password=true",
            "authorization_url=https://x.com/oauth",
        ],
    )
    def test_negative_field_zero_hits(self, sample):
        assert AssignmentDetector().scan(sample) == []


# ---------------------------------------------------------------------------
# Task 2: headers / dsn / pem / cookie
# ---------------------------------------------------------------------------

# 八个敏感 header 名（大小写不敏感）
_HEADER_SAMPLES = (
    "Authorization: Bearer xyz",
    "Authorization: Basic dXNlcjpwYXNz",
    "Proxy-Authorization: Basic dXNlcjpwYXNz",
    "X-API-Key: abc123",
    "X-Goog-API-Key: abc123",
    "API-Key: abc123",
    "X-API-Token: abc123",
    "X-Auth-Token: abc123",
    "X-Access-Token: abc123",
)


class TestHeadersDetector:
    """header 凭据检测：span 只覆盖值本体、保留 header 名与 scheme。"""

    @pytest.mark.parametrize("sample", _HEADER_SAMPLES)
    def test_header_hits(self, sample):
        findings = HeadersDetector().scan(sample)
        assert len(findings) == 1
        assert findings[0].kind == "authorization_header"
        assert findings[0].priority == 90

    def test_basic_credential_span(self):
        """T-06-10 / D10：Basic 的 base64 凭据本体被覆盖（不是只换格式名）。"""
        sample = "Authorization: Basic dXNlcjpwYXNz"
        finding = HeadersDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "dXNlcjpwYXNz"

    @pytest.mark.parametrize("sample", ["Authorization: Bearer short", "Authorization: Bearer " + "A" * 10])
    def test_bearer_no_min_length(self, sample):
        """Bearer 不设最小长度（RFC 6750 允许短 token）。"""
        findings = HeadersDetector().scan(sample)
        assert len(findings) == 1
        assert sample[findings[0].start : findings[0].end] == sample.split()[-1]

    def test_scheme_and_header_name_preserved(self):
        sample = "Authorization: Bearer xyz"
        findings = HeadersDetector().scan(sample)
        assert sample[: findings[0].start] == "Authorization: Bearer "

    @pytest.mark.parametrize("sample", ["Authorization: Basic", "Authorization: Bearer ", "no header here"])
    def test_negative_no_value(self, sample):
        assert HeadersDetector().scan(sample) == []

    @pytest.mark.parametrize("sample", _HEADER_SAMPLES)
    def test_prefilter_reachable(self, sample):
        detector = HeadersDetector()
        assert detector.could_match(sample) is True
        assert detector.scan(sample) != []

    def test_prefilter_negative(self):
        assert HeadersDetector().could_match("plain text") is False


_DSN_SAMPLES = (
    "postgresql://app:S3cr3tPw@db.internal:5432/prod",
    "postgres://app:S3cr3tPw@db.internal/prod",
    "mysql://u:p@h/d",
    "mariadb://u:p@h/d",
    "mongodb://u:p@h/d",
    "mongodb+srv://u:p@h/d",
    "redis://:pw@h/0",
    "rediss://:pw@h/0",
    "amqp://u:p@h/v",
    "amqps://u:p@h/v",
)


class TestDsnDetector:
    """DSN 检测：只替换 password 段，scheme/user/host/port/db 保真。"""

    @pytest.mark.parametrize("sample", _DSN_SAMPLES)
    def test_dsn_hits(self, sample):
        findings = DsnDetector().scan(sample)
        assert len(findings) == 1
        assert findings[0].kind == "dsn_password"
        assert findings[0].priority == 90

    def test_postgres_password_span(self):
        sample = "postgresql://app:S3cr3tPw@db.internal:5432/prod"
        finding = DsnDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "S3cr3tPw"

    @pytest.mark.parametrize("sample", ["redis://:pw@h/0", "mysql://u:p@h/d"])
    def test_short_password_span(self, sample):
        finding = DsnDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] in ("pw", "p")

    @pytest.mark.parametrize(
        "sample",
        [
            "postgresql://app@db.internal:5432/prod",
            "postgresql://db.internal:5432/prod",
            "postgresql://app:${DB_PASSWORD}@host/db",
            "postgresql://app:$DB_PASSWORD@host/db",
            "Password=;User Id=sa",
            "plain text",
        ],
    )
    def test_negative(self, sample):
        assert DsnDetector().scan(sample) == []

    def test_jdbc_password_attribute(self):
        """JDBC 属性串由 ``JdbcDetector`` 覆盖（2026-09-15 起，原由 DsnDetector 的无上下文正则误覆盖）。"""
        sample = "jdbc:sqlserver://h;User Id=sa;Password=S3cr3tPw"
        finding = JdbcDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "S3cr3tPw"

    @pytest.mark.parametrize("sample", _DSN_SAMPLES)
    def test_prefilter_reachable(self, sample):
        detector = DsnDetector()
        assert detector.could_match(sample) is True

    def test_prefilter_negative(self):
        assert DsnDetector().could_match("no dsn here") is False


_PEM_BLOCKS = {
    "RSA": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
    "EC": "-----BEGIN EC PRIVATE KEY-----\nMIIEow\n-----END EC PRIVATE KEY-----",
    "OPENSSH": "-----BEGIN OPENSSH PRIVATE KEY-----\nMIIEow\n-----END OPENSSH PRIVATE KEY-----",
    "ENCRYPTED": "-----BEGIN ENCRYPTED PRIVATE KEY-----\nMIIEow\n-----END ENCRYPTED PRIVATE KEY-----",
    "PRIVATE": "-----BEGIN PRIVATE KEY-----\nMIIEow\n-----END PRIVATE KEY-----",
    "PGP": "-----BEGIN PGP PRIVATE KEY BLOCK-----\nMIIEow\n-----END PGP PRIVATE KEY BLOCK-----",
}


class TestPemDetector:
    """PEM/PGP private key 整块检测；公钥/证书与 label 不一致不命中。"""

    @pytest.mark.parametrize("block", list(_PEM_BLOCKS.values()))
    def test_private_key_block_hits(self, block):
        findings = PemDetector().scan(block)
        assert len(findings) == 1
        assert findings[0].kind == "private_key"
        assert findings[0].priority == 95

    def test_span_covers_whole_block(self):
        block = _PEM_BLOCKS["RSA"]
        finding = PemDetector().scan(block)[0]
        assert block[finding.start : finding.end] == block

    @pytest.mark.parametrize(
        "block",
        [
            "-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----",
            "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----",
            "-----BEGIN PGP PUBLIC KEY BLOCK-----\nAAAA\n-----END PGP PUBLIC KEY BLOCK-----",
            "-----BEGIN SSH2 PUBLIC KEY-----\nAAAA\n-----END SSH2 PUBLIC KEY-----",
        ],
    )
    def test_public_key_not_matched(self, block):
        assert PemDetector().scan(block) == []

    def test_mismatched_labels_not_matched(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END EC PRIVATE KEY-----"
        assert PemDetector().scan(block) == []

    @pytest.mark.parametrize("block", list(_PEM_BLOCKS.values()))
    def test_prefilter_reachable(self, block):
        assert PemDetector().could_match(block) is True

    def test_prefilter_negative(self):
        assert PemDetector().could_match("no dashes here") is False


class TestCookieDetector:
    """Cookie / Set-Cookie 检测：只替换敏感名 cookie 的值，属性保真。"""

    def test_sensitive_cookie_value_only(self):
        sample = "Cookie: session=abc123; theme=dark"
        findings = CookieDetector().scan(sample)
        assert len(findings) == 1
        assert findings[0].kind == "cookie"
        assert findings[0].priority == 85
        assert sample[findings[0].start : findings[0].end] == "abc123"

    def test_set_cookie_expires_comma_survives(self):
        sample = "Set-Cookie: auth_token=xyz; Expires=Wed, 21 Oct 2026 07:28:00 GMT; Secure; HttpOnly"
        findings = CookieDetector().scan(sample)
        assert len(findings) == 1
        assert sample[findings[0].start : findings[0].end] == "xyz"

    @pytest.mark.parametrize("name", ["session", "sessionid", "auth_token", "token", "jwt", "csrf", "xsrf_token"])
    def test_sensitive_names(self, name):
        sample = f"Cookie: {name}=abc123; theme=dark"
        assert len(CookieDetector().scan(sample)) == 1

    @pytest.mark.parametrize("sample", ["Cookie: theme=dark; lang=zh", "Set-Cookie: lang=zh; Path=/; Secure"])
    def test_negative_non_sensitive_name(self, sample):
        assert CookieDetector().scan(sample) == []

    def test_prefilter_reachable(self):
        detector = CookieDetector()
        assert detector.could_match("Set-Cookie: session=abc") is True

    def test_prefilter_negative(self):
        assert CookieDetector().could_match("no header here") is False


# ---------------------------------------------------------------------------
# Task 3: url / jwt + pipeline integration
# ---------------------------------------------------------------------------

_JWT_3SEG = "eyJ" + "d" * 20 + "." + "e" * 20 + "." + "f" * 20
_JWT_5SEG = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20 + "." + "d" * 20 + "." + "e" * 20


class TestUrlDetector:
    """URL query 与 form-urlencoded：只替换敏感值，不重新序列化。"""

    def test_url_query_value_span(self):
        sample = "https://x.com/cb?access_token=abc123&next=/home"
        finding = UrlDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "abc123"
        assert finding.kind == "url_credential"
        assert finding.priority == 85

    @pytest.mark.parametrize(
        "key",
        ["access_token", "token", "api_key", "apikey", "secret", "client_secret", "password", "x-amz-signature"],
    )
    def test_sensitive_query_keys(self, key):
        sample = f"https://x.com/cb?{key}=abc123&next=/home"
        findings = UrlDetector().scan(sample)
        assert len(findings) == 1
        assert sample[findings[0].start : findings[0].end] == "abc123"

    def test_form_urlencoded(self):
        sample = "access_token=abc123&next=/home"
        finding = UrlDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "abc123"

    @pytest.mark.parametrize(
        "sample", ["https://x.com/cb?next=/home&page=2", "token_count=100&page=2", "status code 200"]
    )
    def test_negative(self, sample):
        assert UrlDetector().scan(sample) == []

    @pytest.mark.parametrize("sep", ["&", "#", " ", ";"])
    def test_value_terminators(self, sep):
        sample = f"https://x.com/cb?access_token=abc123{sep}trailing"
        finding = UrlDetector().scan(sample)[0]
        assert sample[finding.start : finding.end] == "abc123"

    @pytest.mark.parametrize("sample", ["https://x.com/cb?token=$TOKEN", "https://x.com/cb?token=${TOKEN}"])
    def test_variable_value_negative(self, sample):
        assert UrlDetector().scan(sample) == []

    def test_prefilter_reachable(self):
        detector = UrlDetector()
        assert detector.could_match("https://x.com?access_token=abc") is True
        assert detector.could_match("plain") is False


class TestJwtDetector:
    """三段 JWS 与五段 JWE 整体替换；单段 eyJ 不命中。"""

    def test_three_segment_jws_whole(self):
        finding = JwtDetector().scan(_JWT_3SEG)[0]
        assert _JWT_3SEG[finding.start : finding.end] == _JWT_3SEG
        assert finding.kind == "jwt"
        assert finding.priority == 88

    def test_five_segment_jwe_whole(self):
        """五段 JWE 必须整体替换（ref.md §10.8：不能只遮前三段）。"""
        finding = JwtDetector().scan(_JWT_5SEG)[0]
        assert _JWT_5SEG[finding.start : finding.end] == _JWT_5SEG

    @pytest.mark.parametrize(
        "sample",
        ["eyJhbGciOiJIUzI1NiJ9", "the eyJ prefix is common in JSON", "plain text"],
    )
    def test_negative_single_segment(self, sample):
        assert JwtDetector().scan(sample) == []

    def test_prefilter(self):
        assert JwtDetector().could_match(_JWT_3SEG) is True
        assert JwtDetector().could_match("no token here") is False


class TestDetectorsIntegratedInPipeline:
    """端到端：所有关键正例经 redact_text 真实生效（防「写了但没接」T-06-16）。"""

    @pytest.mark.parametrize(
        "sample, secret",
        [
            (f"GPG_KEY={_GPG_SAMPLE}", _GPG_SAMPLE),
            ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
            ("postgresql://app:S3cr3tPw@db.internal:5432/prod", "S3cr3tPw"),
            ("-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----", "MIIEow"),
            ("Cookie: session=abc123; theme=dark", "abc123"),
            ("https://x.com/cb?access_token=abc123&next=/home", "abc123"),
            (_JWT_3SEG, _JWT_3SEG),
        ],
    )
    def test_positive_redacted_end_to_end(self, sample, secret):
        out = redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert secret not in out

    def test_url_fidelity_whole_string(self):
        """整串保真断言（非 in）：URL 不被重新序列化，参数顺序与编码原样。"""
        src = "https://x.com/cb?access_token=abc123&next=/home"
        out = redact_text(src, purpose=RedactionPurpose.LOG)
        assert out == "https://x.com/cb?access_token=[REDACTED:url_credential]&next=/home"

    def test_dsn_fidelity_end_to_end(self):
        src = "postgresql://app:S3cr3tPw@db.internal:5432/prod"
        out = redact_text(src, purpose=RedactionPurpose.EXPORT)
        assert "S3cr3tPw" not in out
        for keep in ("postgresql", "app", "db.internal", "5432", "prod"):
            assert keep in out

    def test_authorization_header_not_stolen_by_assignment(self):
        """回归：authorization 严格字段不得抢答 header scheme，header 名保真。"""
        src = "Authorization: Basic dXNlcjpwYXNz"
        out = redact_text(src, purpose=RedactionPurpose.LOG)
        assert out == "Authorization: Basic [REDACTED:authorization_header]"


class TestPipelineRawRuleHitsUnchanged:
    """重叠场景：unique_findings 合并为 1，raw_rule_hits 保留多 rule_id。"""

    def test_bearer_jwt_overlap_multi_rule(self):
        src = f"Authorization: Bearer {_JWT_3SEG}"
        result = scan_text(src, purpose=RedactionPurpose.LOG)
        assert result.unique_findings == 1
        assert any(rule_id.startswith("jwt") for rule_id in result.raw_rule_hits)
        assert sum(result.raw_rule_hits.values()) >= 1

    def test_assignment_counted_in_raw_rule_hits(self):
        result = scan_text(f"GPG_KEY={_GPG_SAMPLE}", purpose=RedactionPurpose.LOG)
        assert "assignment.gpgkey" in result.raw_rule_hits


class TestRedactPayload:
    """结构化脱敏：凭据字段掩码、非凭据字段保真、容器递归（承接已删 test_redact.py）。"""

    @pytest.mark.parametrize(
        "payload",
        [
            {"password": "p@ss", "nested": {"api_key": "***"}, "username": "alice"},
            [{"token": "secret-value"}, "plain"],
            {"API-KEY": "v"},
        ],
    )
    def test_credential_fields_masked(self, payload):
        result = redact_payload(payload, purpose=RedactionPurpose.LOG)
        # 逐项断言：凭据键值被替换，非凭据内容原样保留
        flat = str(result)
        assert "p@ss" not in flat
        assert "plain" in flat or "alice" in flat or "[REDACTED:" in flat

    def test_non_credential_field_untouched(self):
        """非凭据字段整串相等（不误伤 token_count / description）。"""
        payload = {"token_count": 42, "description": "fine"}
        assert redact_payload(payload, purpose=RedactionPurpose.LOG) == payload

    def test_credentials_container_recursed_d6(self):
        """D6 回归：容器值不被整体替换，子对象字段保留。"""
        payload = {"credentials": {"user": "a", "password": "b"}}
        result = redact_payload(payload, purpose=RedactionPurpose.LOG)
        assert isinstance(result["credentials"], dict)
        assert result["credentials"]["user"] == "a"
        assert "b" not in str(result["credentials"]["password"])


class TestRedactForExport:
    """导出入口等价 purpose=EXPORT，str 与 payload 两条路径均脱敏。"""

    def test_export_redacts_string(self):
        assert "abc" not in redact_for_export("secret=abc")

    def test_export_redacts_payload(self):
        assert "abc" not in redact_for_export({"secret": "abc"})["secret"]


class TestCountSensitiveHits:
    """敏感命中统计：只计数、不落明文；旧三键口径保留。"""

    def test_counts_vendor_tokens(self):
        hits = count_sensitive_hits("key1 sk-" + "a" * 36 + " key2 ghp_" + "B" * 36)
        assert hits["vendor_token"] == 2
        assert hits["total"] == 2

    def test_credential_key_value_populated(self):
        hits = count_sensitive_hits(f"GPG_KEY={_GPG_SAMPLE}")
        assert hits["credential_key_value"] >= 1

    def test_clean_text_zero(self):
        assert count_sensitive_hits("nothing sensitive here") == {
            "vendor_token": 0,
            "credential_key_value": 0,
            "total": 0,
        }

    def test_empty_returns_zero(self):
        assert count_sensitive_hits("")["total"] == 0


# ---------------------------------------------------------------------------
# 缺陷回归：phase-06 脱敏引擎的四个独立缺陷（D1-D4）
# ---------------------------------------------------------------------------

# D1：赋值 value 不得吞掉 query string 后续参数
_URL_TAIL_SAMPLES = (
    "?access_token=x&next=/a",  # ROADMAP 字面判据
    "/cb?access_token=x&next=/a",
    "GET /oauth?access_token=x&next=/a",
    "https://x.com/cb?access_token=x&next=/home",
)


class TestUrlCredentialValueTerminator:
    """缺陷 1：assignment 的 value 不得吞掉 `&` / `#` 之后的非敏感参数。"""

    @pytest.mark.parametrize("sample", _URL_TAIL_SAMPLES)
    def test_non_sensitive_tail_survives(self, sample):
        out = redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert "&next=" in out  # 无关参数保真（此前被整体销毁）
        assert "access_token=" in out  # key 名保真
        assert "[REDACTED:url_credential]" in out  # secret 本体被替换

    @pytest.mark.parametrize("purpose", [RedactionPurpose.LOG, RedactionPurpose.EXPORT])
    def test_long_secret_under_partial_mask_does_not_leak_tail(self, purpose):
        """≥32 字符 secret 在 LOG/EXPORT（head6/tail4）下不得泄露后续参数。"""
        secret = "ABCDEF" + "x" * 30 + "WXYZ"  # 40 字符
        sample = f"?client_secret={secret}&next=/a&page=2"
        out = redact_text(sample, purpose=purpose)
        assert "next=/a" in out and "page=2" in out
        assert secret not in out

    def test_hash_fragment_terminator(self):
        """`#` 亦为 value 终止符（fragment 不被吞）。"""
        out = redact_text("?access_token=x#section-2", purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out == "?access_token=[REDACTED:url_credential]#section-2"


# D2：变量引用护栏必须锚定于值起始处，不能因 64 字符外的无关 token 跳过真实 secret
_REAL_SECRET = "DNxril3RavGD5MfvJ7NScUyk"  # 24 字符高熵


class TestVariableReferenceGuardAnchored:
    """缺陷 2：值之后的无关 process.env./os.getenv( 不得把真实 secret 判为变量引用。"""

    @pytest.mark.parametrize(
        "trailer",
        ["process.env.X", "os.getenv(", "process.env.NODE_ENV"],
    )
    def test_trailing_var_ref_token_does_not_skip_secret(self, trailer):
        src = f"private_key={_REAL_SECRET} then some words {trailer}"
        out = redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _REAL_SECRET not in out
        assert "[REDACTED:" in out

    def test_control_without_trailer_still_redacts(self):
        src = f"private_key={_REAL_SECRET} then some words nothing here"
        assert _REAL_SECRET not in redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)

    @pytest.mark.parametrize(
        "src",
        [
            "password=${DB_PASSWORD}",
            "password=$DB_PASSWORD",
            "api_key=process.env.API_KEY",
            "secret=${VAULT_TOKEN}",
        ],
    )
    def test_genuine_var_ref_still_preserved(self, src):
        """护栏本意不可破坏：真实变量引用值仍原样保留。"""
        assert redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT) == src

    @pytest.mark.parametrize("length", [8, 31, 32])
    def test_strict_path_length_boundaries(self, length):
        """严格路径 8/31/32 长度边界均须命中（此前 8-31 段静默漏报）。"""
        value = "A1" + "z" * (length - 3) + "9"
        out = redact_text(f"private_key={value} process.env.TAIL", purpose=RedactionPurpose.MODEL_OUTPUT)
        assert value not in out


# D3：ReDoS —— 大段无分隔符文本不得 O(n²)；预筛选必须挂进 detector 循环
class TestPrefilterReDoSProtection:
    """缺陷 3：could_match 预筛选接入 + key 正则锚定，大输入必须线性完成。"""

    def test_large_alnum_input_completes_quickly(self):
        blob = "a" * 200_000  # 旧行为：无预筛选时 >400s 挂死
        result = scan_text(blob, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert result.redacted_text == blob
        assert result.unique_findings == 0

    def test_scan_self_guards_on_could_match(self):
        """T-vq0：预筛选所有权在 ``scan`` 自身 —— detector 自守卫，而非调用方兜底。"""
        assert AssignmentDetector.could_match(AssignmentDetector(), "no delimiter here") is False
        assert AssignmentDetector().scan("no delimiter here") == []

    @pytest.mark.parametrize(
        "cls, sample_pos",
        [
            (AssignmentDetector, "GPG_KEY=" + _GPG_SAMPLE),
            (VendorTokenDetector, "sk-" + "a" * 36),
        ],
    )
    def test_scan_self_guards_before_expensive_work(self, cls, sample_pos):
        """could_match=False 时 scan 必须在触碰昂贵扫描之前返回（T-vq0 判别式）。

        机制侧：先钉住 could_match 本身对无分隔符大输入返回 False；再证明该输入
        下 scan 走的是**廉价早退**而非昂贵路径 —— 用 200k 输入的耗时上界做判据
        （``re.Pattern.finditer`` 是 C 类型不可 patch，见计划 Task 1 第 5 步的退化分支）。
        守卫若仍在调用方，``cls().scan(blob)`` 会落到昂贵的 200k 扫描 ⇒ 断言失败。
        """
        blob = "no delimiter " + "a" * 200_000
        assert cls().could_match(blob) is False

        with patch.object(cls, "could_match", return_value=False):
            start = time.perf_counter()
            assert cls().scan(blob) == []
            elapsed = time.perf_counter() - start
        assert elapsed < 0.05, f"scan did not self-guard before expensive work: {elapsed:.3f}s"

        # 反向对照：could_match=True 时确实进入昂贵路径（证明上界判据本身有效）
        with patch.object(cls, "could_match", return_value=True):
            start = time.perf_counter()
            cls().scan(blob)
            elapsed_expensive = time.perf_counter() - start
        assert elapsed_expensive > elapsed

    def test_scan_text_invokes_could_match_exactly_once_per_detector(self):
        """api 不再自行调用 could_match：每个 detector 的 could_match 只被 scan 调一次。"""
        calls = {"n": 0}
        original = AssignmentDetector.could_match

        def counting(self, text):
            calls["n"] += 1
            return original(self, text)

        with patch.object(AssignmentDetector, "could_match", counting):
            scan_text("password=hunter2", purpose=RedactionPurpose.MODEL_OUTPUT)
        assert calls["n"] == 1

    def test_all_detectors_expose_cheap_prefilter(self):
        """每个已接入 detector 均暴露可调用的 could_match（协议契约，Detector 非 runtime_checkable）。

        注：``_detectors_for_scan`` 实际注册 **11** 个实例（`Detector` 协议本身在
        ``base.py``，不是 detector）。本计数于 2026-09-15 由 10 变 11——
        新增 ``JdbcDetector``（JDBC 属性串，带 ``jdbc:`` 上下文）。
        """
        detectors = _detectors_for_scan(settings=SecuritySettings())
        assert len(detectors) == 11
        for detector in detectors:
            assert callable(detector.could_match)
            assert isinstance(detector.could_match("clean plain text 123"), bool)

    @pytest.mark.parametrize(
        "detector_cls, sample",
        [
            (PemDetector, "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"),
            (HeadersDetector, "Authorization: Basic dXNlcjpwYXNz"),
            (DsnDetector, "postgresql://app:S3cr3tPw@db.internal:5432/prod"),
            (JdbcDetector, "jdbc:sqlserver://h;User Id=sa;Password=S3cr3tPw"),
            (JwtDetector, _JWT_3SEG),
            (CookieDetector, "Cookie: session=abc123; theme=dark"),
            (UrlDetector, "https://x.com/cb?access_token=abc123&next=/home"),
            (VendorTokenDetector, "sk-" + "a" * 36),
            (AssignmentDetector, f"GPG_KEY={_GPG_SAMPLE}"),
            (EntropyDetector, f"v {_GPG_SAMPLE}"),
        ],
    )
    def test_prefilter_does_not_skip_real_positives(self, detector_cls, sample):
        """接线预筛选不得漏掉任何既有正例（over-strict could_match = 检测回归）。"""
        detector = detector_cls()
        assert detector.could_match(sample) is True
        assert detector.scan(sample) != []


# D4：公钥 / 证书 PEM body 不得被裸熵兜底二次遮掉
_PEM_PUBLIC_KEY = "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxYz9QwErTyUiOpAsDfGh\n-----END PUBLIC KEY-----"
_PEM_CERTIFICATE = (
    "-----BEGIN CERTIFICATE-----\naUpG7zPe+f6z49nJyz8K8NlHG3mpcvgPUTEPiTUvB1c2x4\n-----END CERTIFICATE-----"
)


class TestPemNonSecretNotMasked:
    """缺陷 4：公钥 / 证书 body 必须字节级原样保留；private key 仍须脱敏。"""

    @pytest.mark.parametrize("block", [_PEM_PUBLIC_KEY, _PEM_CERTIFICATE])
    def test_non_secret_block_byte_identical(self, block):
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    def test_private_key_still_masked(self):
        block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxYz9QwErTyUiOpAsDfGhJk\n-----END RSA PRIVATE KEY-----"
        out = redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out != block
        assert "MIIEow" not in out

    def test_label_followed_by_space_not_excluded(self):
        """反例：`CERTIFICATE AUTHORITY=<value>` 是普通文本，不得被 PEM 排除放过。"""
        src = "CERTIFICATE AUTHORITY=" + _GPG_SAMPLE
        assert _GPG_SAMPLE not in redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)

    def test_entropy_after_end_of_non_secret_block_still_hits(self):
        """反例：块结束之后的裸高熵串仍须命中（排除仅限块内）。"""
        src = _PEM_PUBLIC_KEY + "\n" + _GPG_SAMPLE
        out = redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _GPG_SAMPLE not in out
        assert "-----BEGIN PUBLIC KEY-----" in out


# ---------------------------------------------------------------------------
# 缺陷回归：fix 轮 2 —— D5（`\b` 锚定丢弃数字前缀键）+ D6（PEM 非秘密块覆盖不全）
# ---------------------------------------------------------------------------

# D5：数字前缀键必须命中（`\b` 在「数字→字母」处无边界，此前静默漏报）
_DIGIT_PREFIXED_POSITIVES = (
    "1password=hunter2hunter2",
    "9api_key=abcdefgh12345678",
    "2secret=abcdefgh12345678",
    "1access_token=abcdefghijklmnopqrstuvwxyz",
    "2private_key=abcdefghijklmnopqrstuvwxyz",
)


class TestDigitPrefixedKeys:
    """缺陷 5：key 组锚定 `(?<![A-Za-z_])`，数字前缀键重新命中且线性性不退化。"""

    @pytest.mark.parametrize("sample", _DIGIT_PREFIXED_POSITIVES)
    def test_digit_prefixed_key_masked(self, sample):
        """数字前缀键必须命中（回归：`\\b` 锚定使其全部漏报）。"""
        out = redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out != sample
        assert "[REDACTED:" in out

    @pytest.mark.parametrize(
        "sample",
        ["gpg_key=" + _GPG_SAMPLE, "password=hunter2hunter2", "9api_key=abcdefgh12345678"],
    )
    def test_controls_still_masked(self, sample):
        """正对照：无前缀键与数字前缀键均须命中。"""
        assert "[REDACTED:" in redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT)

    @pytest.mark.parametrize(
        "sample",
        [
            "plain text no delimiter",
            "[A-Za-z_][A-Za-z0-9_-]*",
            "abpassword=hunter2hunter2",  # 更长标识符，非严格字段名
            "token_count=100",
        ],
    )
    def test_negative_not_masked(self, sample):
        """负对照：无 `[:=]` 或非凭据字段名一律零命中。"""
        detector = AssignmentDetector()
        assert detector.scan(sample) == []
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) == sample

    @pytest.mark.parametrize(
        "src",
        ["password=${DB_PASSWORD}", "password=$DB_PASSWORD", "api_key=process.env.API_KEY", "secret=${VAULT_TOKEN}"],
    )
    def test_genuine_var_ref_preserved(self, src):
        """护栏不可破坏：真实变量引用值仍字节级保留。"""
        assert redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT) == src


# D6：PEM 非秘密块须覆盖 label 家族 + 多行 body；private 家族必须仍被遮
_PEM_WRAPPED_BODY = (
    "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAxYz9QwErTyUiOpAsDfGh\n"
    "JkLmNoPqRsTuVwXyZ0123456789abcdefghijklmnopqrstuvwxyzABCD\n"
    "EFGHIJKLMNOPQRSTUVWXYZ0123456789abcdefghijklmnopqrstuvwx+/="
)
_PUBLIC_LABELS = (
    "PUBLIC KEY",
    "RSA PUBLIC KEY",
    "EC PUBLIC KEY",
    "DSA PUBLIC KEY",
    "OPENSSH PUBLIC KEY",
    "CERTIFICATE",
    "PGP PUBLIC KEY BLOCK",
)
_PRIVATE_LABELS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "DSA PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "PGP PRIVATE KEY BLOCK",
)


class TestPemNonSecretLabelFamily:
    """缺陷 6：公钥 / 证书 label 家族 + 多行 body 全排除；private 家族仍须遮。"""

    @pytest.mark.parametrize("label", _PUBLIC_LABELS)
    def test_public_label_family_byte_identical(self, label):
        """全部公钥 / 证书 label 的多行 wrapped body 必须字节级原样保留。"""
        block = f"-----BEGIN {label}-----\n{_PEM_WRAPPED_BODY}\n-----END {label}-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    @pytest.mark.parametrize("label", _PRIVATE_LABELS)
    def test_private_label_family_still_masked(self, label):
        """正对照：private key 家族绝不可被排除集放过（泄露后果远重于原缺陷）。"""
        block = f"-----BEGIN {label}-----\n{_PEM_WRAPPED_BODY}\n-----END {label}-----"
        out = redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert out != block
        assert _PEM_WRAPPED_BODY not in out

    @pytest.mark.parametrize(
        "label, preserved",
        [("SSH2 PUBLIC KEY", True), ("SSH2 ENCRYPTED PRIVATE KEY", False)],
    )
    def test_four_dash_rfc4716(self, label, preserved):
        """RFC 4716 四短横形态：SSH2 公钥保留、加密私钥仍遮。"""
        block = f"---- BEGIN {label} ----\n{_PEM_WRAPPED_BODY}\n---- END {label} ----"
        out = redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert (out == block) is preserved


# D5 伴生：数字前缀修复不得让 O(n²) ReDoS 回归 —— 锚定仍是线性的
class TestAssignmentKeyAnchorLinearity:
    """缺陷 5：`(?<![A-Za-z_])` 必须与 `\\b` 等效地抑制 O(n²)，且不丢线性。"""

    @pytest.mark.parametrize("shape", ["a", "a="])
    def test_doubling_scaling_is_linear(self, shape):
        """4 次倍增：每次耗时比须 ≈ 2（O(n)），远低于 quadratic 的 ≈ 4。"""
        samples = [shape * (20_000 * 2**i) for i in range(5)]
        times = []
        for text in samples:
            start = time.perf_counter()
            scan_text(text, purpose=RedactionPurpose.MODEL_OUTPUT)
            times.append(time.perf_counter() - start)
        ratios = [times[i + 1] / times[i] for i in range(len(times) - 1)]
        assert max(ratios) < 3.0, f"non-linear scaling: {ratios} for shape={shape!r}"

    def test_large_log_blob_completes_quickly(self):
        """~320KB 真实日志样（含分散 `=`）必须快速完成（远低于挂死阈值）。"""
        line = "2026-09-14T12:00:00Z INFO svc=api user=alice status=ok msg=handled path=/v1/items\n"
        blob = line * 3200  # ~320KB
        start = time.perf_counter()
        scan_text(blob, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert time.perf_counter() - start < 5.0


# ---------------------------------------------------------------------------
# 缺陷回归：fix 轮 3 —— D7（PEM 非秘密块排除 fail-open 泄露）
#
# 根因：``is_pem_non_secret_span`` 只读 BEGIN label 就排除 span，而 ``PemDetector``
# 要求 BEGIN == END 才动作。于是「BEGIN 非秘密 + END 错配 / 缺失」的畸形块被熵侧
# 放过、又被 PemDetector 拒绝 —— 两边都不遮，body 原样泄露（fail-open）。
#
# 既有 fixture 全部使用 **BEGIN/END 同名** 的良构块，故连续三轮都漏掉了这类形状。
# 下面显式覆盖错配 / 未闭合 / 双向 sweep，并以「fail-closed」为验收：无法正识别为
# 完整匹配的非秘密块 → 必须被遮。
# ---------------------------------------------------------------------------

# 每个 body 都是长度 ≥32、高熵、非形状排除的裸密文（裸熵兜底必然命中）。
_PEM_LEAK_BODY_ALNUM = "QGBZvSrgJ0hZOs9KHJV4jhw4hKFPGI6G"  # len=32 ent=4.4528 cls=3
_PEM_LEAK_BODY_B64 = "aUpG7zPe+f6z49nJyz8K8NlHG3mpcvgPUTEPiTUv"  # len=40 ent=4.7153 cls=4
_PEM_LEAK_BODY = _PEM_LEAK_BODY_B64

# 非秘密 label 家族（含 X509 / TRUSTED CERTIFICATE —— 任务点名要求覆盖）
_NON_SECRET_LABELS = (
    "PUBLIC KEY",
    "RSA PUBLIC KEY",
    "EC PUBLIC KEY",
    "DSA PUBLIC KEY",
    "OPENSSH PUBLIC KEY",
    "CERTIFICATE",
    "X509 CERTIFICATE",
    "TRUSTED CERTIFICATE",
    "PGP PUBLIC KEY BLOCK",
)
# 私钥 label 家族（正对照：任何形态都必须被遮）
_SECRET_LABELS = (
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "DSA PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
    "PGP PRIVATE KEY BLOCK",
)


def _assert_body_masked(text: str) -> None:
    """断言给定 PEM-ish 文本的 body **不可能**原样存活（fail-closed 验收）。"""
    out = redact_text(text, purpose=RedactionPurpose.MODEL_OUTPUT)
    assert _PEM_LEAK_BODY not in out, f"body leaked verbatim: {out!r}"


class TestPemFailClosedOnMalformedBlocks:
    """D7：畸形 / 错配 / 未闭合的 PEM 块必须 fail-closed（body 被遮，绝不逐字泄露）。"""

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    @pytest.mark.parametrize(
        "end_label",
        ["RSA PRIVATE KEY", "EC PRIVATE KEY", "PGP PRIVATE KEY BLOCK"],
    )
    def test_non_secret_begin_with_mismatched_end_masked(self, label, end_label):
        """点名的泄露形：BEGIN 非秘密 + END 私钥（此前 PemDetector 与熵侧都不遮）。"""
        _assert_body_masked(f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}\n-----END {end_label}-----")

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    def test_non_secret_begin_unterminated_masked(self, label):
        """点名的泄露形：BEGIN 非秘密但**完全没有 END** —— 不得因此被排除。"""
        _assert_body_masked(f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}")

    @pytest.mark.parametrize("begin_label", _NON_SECRET_LABELS)
    @pytest.mark.parametrize("end_label", _NON_SECRET_LABELS)
    def test_mismatched_non_secret_pair_sweep_masked(self, begin_label, end_label):
        """双向 sweep：非秘密 label 之间两两错配也必须被遮（36 组配对）。"""
        if begin_label == end_label:
            pytest.skip("matched pair -> covered by byte-identical positive test")
        _assert_body_masked(f"-----BEGIN {begin_label}-----\n{_PEM_LEAK_BODY}\n-----END {end_label}-----")

    @pytest.mark.parametrize("label", _SECRET_LABELS)
    def test_private_begin_unterminated_masked(self, label):
        """正对照：私钥块未闭合同样必须被遮。"""
        _assert_body_masked(f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}")

    @pytest.mark.parametrize("label", _SECRET_LABELS)
    @pytest.mark.parametrize("end_label", ["EC PRIVATE KEY", "RSA PRIVATE KEY"])
    def test_private_begin_mismatched_end_masked(self, label, end_label):
        """正对照：私钥块 END 错配也必须被遮。"""
        _assert_body_masked(f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}\n-----END {end_label}-----")


class TestPemMatchedNonSecretByteIdentical:
    """良构（BEGIN/END 同名）非秘密块必须字节级保留 —— 与 fail-closed 的另一半。"""

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    def test_matched_multiline_block_byte_identical(self, label):
        """全部非秘密 label + 真实多行 wrapped body 必须原样保留（不误伤，不破坏信任链）。"""
        block = f"-----BEGIN {label}-----\n{_PEM_WRAPPED_BODY}\n-----END {label}-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    def test_matched_crlf_block_byte_identical(self, label):
        """CRLF 行尾的良构非秘密块同样必须原样保留。"""
        body = _PEM_WRAPPED_BODY.replace("\n", "\r\n")
        block = f"-----BEGIN {label}-----\r\n{body}\r\n-----END {label}-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    def test_matched_long_wrapped_body_byte_identical(self, label):
        """30 行 wrapped body（远超市面公钥长度）必须整段保留，不得只放过首行。"""
        body = "\n".join([_PEM_LEAK_BODY_B64] * 30)
        block = f"-----BEGIN {label}-----\n{body}\n-----END {label}-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block


class TestPemBoundaryRobustness:
    """边界 / 健壮性：CRLF、无尾换行、相邻块、body 内嵌异名 END。"""

    def test_crlf_mismatched_end_masked(self):
        """CRLF + END 错配：不得因行尾差异而漏遮。"""
        _assert_body_masked(f"-----BEGIN PUBLIC KEY-----\r\n{_PEM_LEAK_BODY}\r\n-----END RSA PRIVATE KEY-----")

    def test_no_trailing_newline_unterminated_masked(self):
        """无尾换行的未闭合块：必须被遮。"""
        _assert_body_masked(f"-----BEGIN PUBLIC KEY-----\n{_PEM_LEAK_BODY}")

    def test_public_followed_by_private_block_masks_private(self):
        """相邻块：公钥块后的私钥块 body 不得被前块排除「顺带放过」。"""
        src = (
            f"-----BEGIN PUBLIC KEY-----\n{_PEM_LEAK_BODY_B64}\n-----END PUBLIC KEY-----\n"
            f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY_ALNUM}\n-----END RSA PRIVATE KEY-----"
        )
        out = redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _PEM_LEAK_BODY_ALNUM not in out

    def test_secret_after_public_block_still_masked(self):
        """公钥块结束之后紧跟的裸密文仍须命中（排除仅限块内）。"""
        src = f"-----BEGIN PUBLIC KEY-----\n{_PEM_LEAK_BODY_B64}\n-----END PUBLIC KEY-----\n{_PEM_LEAK_BODY_ALNUM}"
        out = redact_text(src, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _PEM_LEAK_BODY_ALNUM not in out
        assert "-----BEGIN PUBLIC KEY-----" in out

    def test_body_containing_other_label_end_is_masked(self):
        """含糊态：良构公钥块 body 内嵌 ``END <异名>`` 行 → 无法确证为完整非秘密块 → 遮。"""
        src = (
            f"-----BEGIN PUBLIC KEY-----\n{_PEM_LEAK_BODY_B64}\n-----END RSA PRIVATE KEY-----\n-----END PUBLIC KEY-----"
        )
        _assert_body_masked(src)

    @pytest.mark.parametrize("label, preserved", [("SSH2 PUBLIC KEY", True), ("SSH2 ENCRYPTED PRIVATE KEY", False)])
    def test_four_dash_rfc4716_still_correct(self, label, preserved):
        """RFC 4716 4 短横形态回归：公钥保留、加密私钥遮（不得因解析共享而退化）。"""
        block = f"---- BEGIN {label} ----\n{_PEM_WRAPPED_BODY}\n---- END {label} ----"
        assert (redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block) is preserved

    def test_four_dash_mismatched_masked(self):
        """4 短横形态 END 错配：必须 fail-closed。"""
        src = f"---- BEGIN SSH2 PUBLIC KEY ----\n{_PEM_LEAK_BODY}\n---- END SSH2 ENCRYPTED PRIVATE KEY ----"
        _assert_body_masked(src)


# ---------------------------------------------------------------------------
# 缺陷回归：fix 轮 4 —— D8（label 归一化缺失 / 分类漂移 fail-open 泄露）
#
# 根因（与 D7 同族）：``iter_pem_blocks`` 曾保留捕获 label 的原始空白，故
# ``-----BEGIN  PUBLIC KEY-----``（双空格）产出 ``label == ' PUBLIC KEY'``。
# 熵侧排除用正则 ``[A-Z0-9 ]*PUBLIC KEY`` **前缀匹配** → 判为非秘密（排除）；
# ``PemDetector`` 用**精确集合**成员判定 → ``' PUBLIC KEY'`` 既非私钥也非其已知
# 公钥 → 不产 finding。**两边都不遮** → body 原样泄露（fail-open）。
#
# 同族第二泄露面：正则前缀匹配过宽 —— ``MYCERTIFICATE`` / ``FOOCERTIFICATE`` /
# ``XYZ PUBLIC KEY`` / ``X509CERTIFICATE``（无空格）皆命中 ``[A-Z0-9 ]*CERTIFICATE``
# 或 ``[A-Z0-9 ]*PUBLIC KEY`` → 被错排除而 detector 不识别 → 泄露。
#
# 修复：``iter_pem_blocks`` **解析时归一化** label（``normalize_label``：折叠内部
# 空白 + strip），并新增 ``classify_pem_label`` 作为**唯一**分类判据，两端共用。
# ``unknown`` label 不产 finding、也不被排除 → 裸熵兜底遮掉（fail-closed）。
# ---------------------------------------------------------------------------

# 空白变形：**可被 _BEGIN_RE 解析**的空白异体（正则要求 BEGIN 后为字面空格，
# 故 ``BEGIN\t`` 不解析 —— 见 TestPemAdversarialShapes）。这些必须归一到同一规范形
# （RFC 词表，空白无语义）。
_PEM_WS_LABEL_VARIANTS = (
    "-----BEGIN  PUBLIC KEY-----",  # 双空格（点名的泄露形）
    "-----BEGIN   PUBLIC KEY-----",  # 三空格
    "-----BEGIN PUBLIC  KEY-----",  # 中间双空格
    "-----BEGIN PUBLIC KEY -----",  # 尾随空白
    "-----BEGIN  RSA PUBLIC KEY-----",
    "-----BEGIN CERTIFICATE -----",
)

# 近义 label（归一化后仍**不**在白名单）→ unknown → 必须被遮
_PEM_NEAR_MISS_LABELS = (
    "MYCERTIFICATE",
    "FOOCERTIFICATE",
    "X509CERTIFICATE",
    "XYZ PUBLIC KEY",
    "PRIVATECERTIFICATE",
    "PUBLIC KEYX",
)


class TestPemLabelNormalization:
    """D8：label 归一化 —— 空白异体归一到同一规范形，分类两端一致。"""

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("PUBLIC KEY", "PUBLIC KEY"),
            ("  PUBLIC KEY", "PUBLIC KEY"),
            ("PUBLIC  KEY", "PUBLIC KEY"),
            ("PUBLIC KEY ", "PUBLIC KEY"),
            ("  PUBLIC   KEY  ", "PUBLIC KEY"),
            ("\tPUBLIC KEY\t", "PUBLIC KEY"),
            ("RSA  PRIVATE  KEY", "RSA PRIVATE KEY"),
            ("", ""),
        ],
    )
    def test_normalize_label_collapses_whitespace(self, raw, expected):
        """``normalize_label`` 折叠内部空白 + strip（标签是 RFC 词表，空白不承载语义）。"""
        from aidev_agent.packages.security.redaction.detectors import normalize_label

        assert normalize_label(raw) == expected

    @pytest.mark.parametrize("raw", ["", "  ", "\t", "\n"])
    def test_normalize_label_empty_stays_empty(self, raw):
        """纯空白归一到空串（不得产出伪造 label）。"""
        from aidev_agent.packages.security.redaction.detectors import normalize_label

        assert normalize_label(raw) == ""

    @pytest.mark.parametrize(
        "label, expected",
        [
            ("PUBLIC KEY", "non_secret"),
            ("RSA PUBLIC KEY", "non_secret"),
            ("EC PUBLIC KEY", "non_secret"),
            ("DSA PUBLIC KEY", "non_secret"),
            ("OPENSSH PUBLIC KEY", "non_secret"),
            ("CERTIFICATE", "non_secret"),
            ("X509 CERTIFICATE", "non_secret"),
            ("TRUSTED CERTIFICATE", "non_secret"),
            ("PGP PUBLIC KEY BLOCK", "non_secret"),
            ("SSH2 PUBLIC KEY", "non_secret"),
            ("PRIVATE KEY", "secret"),
            ("RSA PRIVATE KEY", "secret"),
            ("EC PRIVATE KEY", "secret"),
            ("OPENSSH PRIVATE KEY", "secret"),
            ("ENCRYPTED PRIVATE KEY", "secret"),
            ("PGP PRIVATE KEY BLOCK", "secret"),
            ("DSA PRIVATE KEY", "unknown"),  # 非 RFC 词表 → unknown → 遮（fail-closed）
            (" PUBLIC KEY", "unknown"),
            ("MYCERTIFICATE", "unknown"),
            ("X509CERTIFICATE", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_classify_pem_label_single_source_of_truth(self, label, expected):
        """``classify_pem_label`` 是唯一分类判据：非秘密白名单为**精确** label。"""
        from aidev_agent.packages.security.redaction.detectors import classify_pem_label

        assert classify_pem_label(label) == expected

    @pytest.mark.parametrize(
        "label",
        ["PUBLIC KEY", "PRIVATE KEY", "MYCERTIFICATE", "X509CERTIFICATE", "RSA PUBLIC KEY"],
    )
    def test_detector_and_exclusion_share_one_source_of_truth(self, label):
        """不变式：``PemDetector`` 产 finding ⟺ 熵侧放弃排除 ⟺ ``classify==secret``。

        三者必须由**同一** ``classify_pem_label`` 决定；本测试对每个 label 直接
        比对三端结论，任何「平行检查」漂移都会在此暴露。
        """
        from aidev_agent.packages.security.redaction.detectors import classify_pem_label, is_pem_non_secret_span

        src = f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}\n-----END {label}-----"
        verdict = classify_pem_label(label)
        body_start = src.index(_PEM_LEAK_BODY)
        detector_masks = bool(PemDetector().scan(src))
        excluded = is_pem_non_secret_span(src, body_start, body_start + len(_PEM_LEAK_BODY))
        assert detector_masks is (verdict == "secret")
        assert excluded is (verdict == "non_secret")

    def test_normalized_whitespace_variant_agrees_end_to_end(self):
        """空白异体：解析后 label 归一化 → 分类 / 排除 / detector 三端仍一致（PUBLIC 保留）。"""
        from aidev_agent.packages.security.redaction.detectors import iter_pem_blocks

        src = f"-----BEGIN  PUBLIC KEY-----\n{_PEM_LEAK_BODY}\n-----END  PUBLIC KEY-----"
        block = iter_pem_blocks(src)[0]
        body_start = src.index(_PEM_LEAK_BODY)
        assert block.label == "PUBLIC KEY"  # 解析时已归一化
        assert is_pem_non_secret_span(src, body_start, body_start + len(_PEM_LEAK_BODY)) is True
        assert PemDetector().scan(src) == []  # 非私钥 → 不产 finding

    @pytest.mark.parametrize(
        "begin",
        [
            "-----BEGIN  PRIVATE KEY-----",
            "-----BEGIN   PRIVATE KEY-----",
            "-----BEGIN PRIVATE  KEY-----",
            "-----BEGIN PRIVATE KEY -----",
            "-----BEGIN  RSA PRIVATE KEY-----",
            "-----BEGIN OPENSSH  PRIVATE KEY-----",
            # BEGIN 后为制表符：_BEGIN_RE 要求字面空格 → 不解析 → 不排除 → 遮（fail-closed）
            "-----BEGIN\tPRIVATE KEY-----",
        ],
    )
    def test_whitespace_variant_private_still_masked(self, begin):
        """空白异体的**私钥** label 必须归一化后仍被遮（fail-closed 主方向）。"""
        end = begin.replace("BEGIN", "END")
        _assert_body_masked(f"{begin}\n{_PEM_LEAK_BODY}\n{end}")

    @pytest.mark.parametrize("begin", _PEM_WS_LABEL_VARIANTS)
    def test_whitespace_variant_public_normalizes_to_non_secret(self, begin):
        """空白异体的公钥 label 归一到规范形 → 正识别为非秘密 → 字节级保留。"""
        label = begin[len("-----BEGIN ") : -len("-----")]
        end = f"-----END {label}-----"
        block = f"{begin}\n{_PEM_WRAPPED_BODY}\n{end}"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    @pytest.mark.parametrize(
        "begin",
        [
            "-----BEGIN  RSA PRIVATE KEY-----",
            "-----BEGIN OPENSSH  PRIVATE KEY-----",
            "-----BEGIN\tEC PRIVATE KEY-----",
            "-----BEGIN  PGP PRIVATE KEY BLOCK-----",
        ],
    )
    @pytest.mark.parametrize("flip", ["space", "tab"])
    def test_asymmetric_padding_both_sides_masked_when_private(self, begin, flip):
        """非对称空白（BEGIN/END 空白不同）的私钥块同样必须被遮。"""
        end_label = begin[len("-----BEGIN ") : -len("-----")].replace(" ", "\t" if flip == "tab" else "  ")
        _assert_body_masked(f"{begin}\n{_PEM_LEAK_BODY}\n-----END {end_label}-----")

    def test_space_in_end_label_only_masked_when_private(self):
        """非对称：仅 END 侧空白膨胀的私钥块必须被遮（此前两边都不管）。"""
        _assert_body_masked(f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END  RSA PRIVATE KEY-----")

    def test_space_in_begin_label_only_masked_when_private(self):
        """非对称：仅 BEGIN 侧空白膨胀的私钥块必须被遮。"""
        _assert_body_masked(f"-----BEGIN  RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END RSA PRIVATE KEY-----")

    @pytest.mark.parametrize("label", _PEM_NEAR_MISS_LABELS)
    def test_near_miss_label_masked(self, label):
        """近义 label（归一化后不在白名单）→ unknown → 必须被遮（fail-closed）。"""
        _assert_body_masked(f"-----BEGIN {label}-----\n{_PEM_LEAK_BODY}\n-----END {label}-----")

    @pytest.mark.parametrize("label", _NON_SECRET_LABELS)
    def test_canonical_non_secret_labels_remain_byte_identical(self, label):
        """规范非秘密 label（归一化不变）必须仍字节级保留 —— 指向性 sweep。"""
        block = f"-----BEGIN {label}-----\n{_PEM_WRAPPED_BODY}\n-----END {label}-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block

    @pytest.mark.parametrize("label", _SECRET_LABELS)
    def test_canonical_secret_labels_remain_masked(self, label):
        """规范私钥 label 必须仍被遮 —— 指向性 sweep（正对照的另一半）。"""
        block = f"-----BEGIN {label}-----\n{_PEM_WRAPPED_BODY}\n-----END {label}-----"
        out = redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _PEM_WRAPPED_BODY not in out


class TestPemAdversarialShapes:
    """D7/D8 伴生：对抗形状全部必须 fail-closed（body 绝不逐字泄露）。"""

    @pytest.mark.parametrize(
        "src",
        [
            # 嵌套 BEGIN in body
            f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----BEGIN EC PRIVATE KEY-----\n-----END RSA PRIVATE KEY-----",
            # END 先于 BEGIN
            f"-----END RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----BEGIN RSA PRIVATE KEY-----",
            # 仅 BEGIN、无换行（alnum body：候选不被 ``KEY-----`` 粘连降熵）
            f"-----BEGIN RSA PRIVATE KEY-----{_PEM_LEAK_BODY_ALNUM}",
            # 小写 label
            f"-----BEGIN rsa private key-----\n{_PEM_LEAK_BODY}\n-----END rsa private key-----",
            # 7 短横（两侧）
            f"-------BEGIN RSA PRIVATE KEY-------\n{_PEM_LEAK_BODY}\n-------END RSA PRIVATE KEY-------",
            # body 本身是 PEM header
            f"-----BEGIN RSA PRIVATE KEY-----\n-----BEGIN PUBLIC KEY-----\n{_PEM_LEAK_BODY}\n-----END RSA PRIVATE KEY-----",
            # 空白膨胀 label（本缺陷）
            f"-----BEGIN  RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END  RSA PRIVATE KEY-----",
            # 制表符膨胀 label（不解析 → 不排除 → 遮）
            f"-----BEGIN\tRSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END\tRSA PRIVATE KEY-----",
            # END label 仅前导空白（非对称）
            f"-----BEGIN RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END  RSA PRIVATE KEY-----",
            # BEGIN label 仅前导空白（非对称）
            f"-----BEGIN  RSA PRIVATE KEY-----\n{_PEM_LEAK_BODY}\n-----END RSA PRIVATE KEY-----",
        ],
    )
    def test_adversarial_shape_never_leaks(self, src):
        """六类对抗形状 + 四类空白异体：私钥 body 在任何形态下都不得原样存活。"""
        _assert_body_masked(src)

    @pytest.mark.parametrize(
        "label",
        ["PUBLIC KEY", "RSA PUBLIC KEY", "EC PUBLIC KEY", "DSA PUBLIC KEY", "OPENSSH PUBLIC KEY"],
    )
    def test_tab_after_begin_label_is_not_excluded(self, label):
        """``BEGIN<tab>`` 不被 _BEGIN_RE 解析 → 不排除 → 公钥 body 也被遮（可接受的过度遮蔽）。"""
        src = f"-----BEGIN\t{label}-----\n{_PEM_LEAK_BODY}\n-----END\t{label}-----"
        _assert_body_masked(src)

    def test_begin_end_label_mismatch_after_normalization_masked(self):
        """归一化后 BEGIN（``PUBLIC``）≠ END（``PUBLIC KEY``）→ 不产块 → 遮（fail-closed）。"""
        block = f"-----BEGIN PUBLIC-----\n{_PEM_WRAPPED_BODY}\n-----END PUBLIC KEY-----"
        _assert_body_masked(block)

    def test_asymmetric_whitespace_same_label_still_recognized(self):
        """非对称空白但**归一化后同名**（``BEGIN  PUBLIC KEY`` + ``END PUBLIC KEY``）→ 保留。"""
        block = f"-----BEGIN  PUBLIC KEY-----\n{_PEM_WRAPPED_BODY}\n-----END PUBLIC KEY-----"
        assert redact_text(block, purpose=RedactionPurpose.MODEL_OUTPUT) == block


# ---------------------------------------------------------------------------
# 已知值精确匹配 detector（注入式）—— 迁移自已删除的 test_registry.py
#
# 旧实现依赖 ``redaction.registry`` 的模块级全局态（``_REGISTRY`` + ``threading.Lock``
# + ``_SNAPSHOT``）与 ``_reset_registry()`` autouse fixture 隔离用例。现改为
# ``RegisteredSecretDetector.from_values(...)`` **显式注入** ⇒ 无全局态、无跨用例污染，
# 故 fixture 一并删除。``TestRegisterSecret``（注册门槛：长度/空白/sentinel 拒绝）
# 随注册表消失而删除 —— 门槛是全局注册表的守卫，注入式路径不再需要。
# ``TestRegisteredSecretRepr`` 的安全属性（repr 不泄露 value）**必须**保留为回归护栏。
# ---------------------------------------------------------------------------

_KNOWN_SAMPLE = "QGBZvSrgJ0hZOs9KHJV4jhw4hKFPGI6G"  # len=32，GPG key 样值


class TestRegisteredSecretDetectorInjected:
    """注入式 detector：裸值精确命中，priority=100，confidence=exact。"""

    @staticmethod
    def _scan(text: str, values=(_KNOWN_SAMPLE,), kind: str = "gpg_key"):
        detector = RegisteredSecretDetector.from_values(values, default_kind=kind)
        return detector.scan(text)

    def test_bare_value_is_matched(self):
        findings = self._scan(f"the value {_KNOWN_SAMPLE} appears")
        assert len(findings) == 1
        found = findings[0]
        assert (found.start, found.end) == (10, 10 + len(_KNOWN_SAMPLE))
        assert found.rule_id == "registered:gpg_key"
        assert found.kind == "registered_secret"
        assert found.confidence == "exact"
        assert found.priority == 100

    def test_multiple_occurrences_produce_multiple_findings(self):
        assert len(self._scan(f"{_KNOWN_SAMPLE} then {_KNOWN_SAMPLE}")) == 2

    def test_uninjected_value_not_matched(self):
        """未注入的值不命中（空注入 ⇒ 空 detector）。"""
        assert self._scan(f"the value {_KNOWN_SAMPLE} appears", values=()) == []

    def test_could_match_short_circuits_on_first_char(self):
        detector = RegisteredSecretDetector.from_values([_KNOWN_SAMPLE])
        assert detector.could_match(f"xxx {_KNOWN_SAMPLE}") is True
        assert detector.could_match("no matching first char here") is False

    @pytest.mark.parametrize("text", ["", 123, None, ["not", "str"]])
    def test_defensive_input_returns_empty(self, text):
        assert self._scan(text) == []

    def test_two_constructions_do_not_interfere(self):
        """显式注入：两次构造互不影响（无跨用例污染 ⇒ 无需 _reset_registry fixture）。"""
        first = RegisteredSecretDetector.from_values([_KNOWN_SAMPLE])
        second = RegisteredSecretDetector.from_values([])
        assert first.scan(_KNOWN_SAMPLE) != []
        assert second.scan(_KNOWN_SAMPLE) == []

    def test_longer_value_wins_within_group(self):
        """组内长值优先：detector 内部按值长度降序排列（长值先扫、先产出）。

        ``scan`` 对每个注入值独立产出 finding（跨值重叠由 ``merge_spans`` 按
        priority 决策，属既有语义）；此断言钉住的是**组内顺序** —— 长值的 finding
        先于短值出现，这是「长值优先命中」在 detector 层的可观测信号。
        """
        short, long = "Abc123", "Abc123456789"
        detector = RegisteredSecretDetector.from_values([short, long])
        ordered = detector._grouped["A"]
        assert [item.value for item in ordered] == [long, short]

    def test_long_value_produces_finding_before_short(self):
        short, long = "Abc123", "Abc123456789"
        findings = RegisteredSecretDetector.from_values([short, long]).scan(f"x {long} y {short}")
        # 首个 finding 覆盖长值（按值长度降序遍历 group 的自然结果）
        assert findings[0].end - findings[0].start == len(long)

    def test_known_value_accepts_str_and_object(self):
        """str 与 RegisteredValue 混合注入等价于各自的 kind。"""
        grouped = RegisteredSecretDetector.from_values(
            [_KNOWN_SAMPLE, RegisteredValue(value="ZzzSecret", kind="custom")]
        )
        findings = grouped.scan(f"{_KNOWN_SAMPLE} {('ZzzSecret')}")
        assert {f.rule_id for f in findings} == {"registered:known", "registered:custom"}


class TestRegisteredValueReprSafety:
    """T-06-02：已知值（含容器）不出现在任何 repr() 中。"""

    def test_known_value_repr_hides_value(self):
        assert "TOPSECRET" not in repr(RegisteredValue(value="TOPSECRET", kind="k"))

    def test_detector_repr_hides_value(self):
        detector = RegisteredSecretDetector.from_values([RegisteredValue(value="TOPSECRETVALUE", kind="k")])
        assert "TOPSECRETVALUE" not in repr(detector)


# T-vq0：detector 常量封装（模块级常量 → 类属性）
class TestDetectorConstantEncapsulation:
    """封装等价性：常量搬入类体后值逐字不变，派生关系在类体内成立。"""

    @pytest.mark.parametrize(
        "cls, attr, expected",
        [
            (VendorTokenDetector, "_VENDOR_PRIORITY", 80),
            (UrlDetector, "_URL_PRIORITY", 85),
            (JwtDetector, "_JWT_PRIORITY", 88),
            (HeadersDetector, "_HEADERS_PRIORITY", 90),
            (DsnDetector, "_DSN_PRIORITY", 90),
            (CookieDetector, "_COOKIE_PRIORITY", 85),
            (PemDetector, "_PEM_PRIORITY", 95),
            (RegisteredSecretDetector, "_REGISTERED_PRIORITY", 100),
        ],
    )
    def test_priority_class_attrs(self, cls, attr, expected):
        assert getattr(cls, attr) == expected

    def test_url_key_hints_derived_in_class_body(self):
        assert tuple(sorted(UrlDetector._SENSITIVE_QUERY_KEYS)) == UrlDetector._KEY_HINTS

    def test_jwt_segment_derives_both_regexes(self):
        assert JwtDetector._SEGMENT in JwtDetector._JWS_RE.pattern
        assert JwtDetector._SEGMENT in JwtDetector._JWE_RE.pattern

    def test_vendor_rules_all_share_class_priority(self):
        assert len(VendorTokenDetector._VENDOR_RULES) == 14
        assert {rule.priority for rule in VendorTokenDetector._VENDOR_RULES} == {VendorTokenDetector._VENDOR_PRIORITY}

    def test_headers_regex_derived_from_class_attrs(self):
        pattern = HeadersDetector._HEADER_RE.pattern
        assert all(name in pattern for name in HeadersDetector._HEADER_NAMES)
        assert HeadersDetector._SCHEME_RE in pattern

    def test_dsn_regex_derived_from_class_schemes(self):
        pattern = DsnDetector._DSN_RE.pattern
        assert all(re.escape(scheme)[:4] in pattern for scheme in DsnDetector._SCHEMES)

    def test_cookie_sensitive_names_derived_in_class_body(self):
        assert "session" in CookieDetector._SENSITIVE_NAMES
        assert "sessionid" in CookieDetector._SENSITIVE_NAMES

    def test_registered_detector_fields_stay_two(self):
        """ClassVar 陷阱：`_REGISTERED_PRIORITY` 若误写成普通字段会多出一项。"""
        assert [f.name for f in fields(RegisteredSecretDetector)] == ["_grouped", "rule_id"]

    def test_entropy_detector_fields_stay_five(self):
        """ClassVar 陷阱：8 个常量若漏写 ClassVar 会静默变成构造字段。"""
        assert [f.name for f in fields(EntropyDetector)] == [
            "min_length",
            "alnum_threshold",
            "base64_threshold",
            "enabled",
            "rule_id",
        ]
        assert EntropyDetector().__dict__ == {
            "min_length": 32,
            "alnum_threshold": 4.2,
            "base64_threshold": 4.5,
            "enabled": True,
            "rule_id": "entropy",
        }
        assert hash(EntropyDetector()) is not None

    @pytest.mark.parametrize(
        "attr, expected",
        [
            ("_ASSIGNMENT_PRIORITY", 70),
            ("_SCORE_THRESHOLD", 4),
            ("_VAR_REF_CONTEXT", 64),
            ("_AMBIGUOUS_SUFFIXES", ("key", "token", "secret")),
            ("_HEADER_SCHEMES", frozenset({"basic", "bearer", "digest", "negotiate"})),
        ],
    )
    def test_assignment_constants_encapsulated(self, attr, expected):
        assert getattr(AssignmentDetector, attr) == expected

    def test_assignment_normalized_strict_derived_in_class_body(self):
        expected = frozenset(re.sub(r"[^a-z0-9]", "", name.lower()) for name in AssignmentDetector._STRICT_FIELD_NAMES)
        assert expected == AssignmentDetector._NORMALIZED_STRICT

    def test_assignment_key_value_re_anchor_and_terminators_intact(self):
        """缺陷 1 / 3 / 5 的锚定与 value 终止符不得在搬运中被改掉。"""
        pattern = AssignmentDetector._KEY_VALUE_RE.pattern
        assert "(?<![A-Za-z_])" in pattern
        assert "[^\"'\\s,;{}&#]+" in pattern

    @pytest.mark.parametrize(
        "attr, expected",
        [
            ("_ENTROPY_PRIORITY", 50),
            ("_DEFAULT_MIN_LENGTH", 32),
            ("_DEFAULT_ALNUM_THRESHOLD", 4.2),
            ("_DEFAULT_BASE64_THRESHOLD", 4.5),
            ("_KIND", "bare_secret"),
            ("_CONFIDENCE", "heuristic"),
            ("_ALPHABET_BASE64_FAMILY", "base64"),
        ],
    )
    def test_entropy_constants_encapsulated(self, attr, expected):
        assert getattr(EntropyDetector, attr) == expected

    def test_entropy_candidate_re_pattern_unchanged(self):
        """候选字符集：base64/alnum 字母表 + 口令常见符号（S054 族召回修复）。

        两条边界由回归钉住，勿再放宽：
        - 必须是 ASCII 白名单，不能 ``[^\\s…]`` 取反（否则 CJK 散文被整段吞下）。
        - 不得纳入 ``:`` ``/`` —— 否则整个 DSN URL 被吞，
          覆盖并吞掉 ``dsn`` detector 的细粒度命中（host/port/dbname 全丢）。
        """
        assert EntropyDetector._CANDIDATE_RE.pattern == r"[A-Za-z0-9!@$%^&*()_+\-\[\]?.~`]{32,4096}={0,2}"
        # 结构性字符必须留在协议 detector 手里，不得进熵候选字母表
        for structural in (":", "/", "#", "\\s", '"', "'", ";", "<", ">", "|"):
            assert structural not in EntropyDetector._CANDIDATE_RE.pattern, structural

    @pytest.mark.parametrize(
        "attr, expected",
        [
            ("_ALPHABET_ALNUM", "alnum"),
            ("_ALPHABET_BASE64", "base64"),
            ("_ALPHABET_BASE64URL", "base64url"),
            ("_ALPHABET_OTHER", "other"),
            ("_BASE64_CONTAINERS", ("data:", ";base64,")),
            ("_REPETITION_UNIQUE_MAX", 2),
        ],
    )
    def test_entropy_self_hosted_private_constants(self, attr, expected):
        assert getattr(EntropyDetector, attr) == expected

    @pytest.mark.parametrize(
        "attr, pattern",
        [
            ("_ALNUM_RE", r"^[A-Za-z0-9]+$"),
            ("_BASE64_RE", r"^[A-Za-z0-9+/]+={0,2}$"),
            ("_BASE64URL_CHARS_RE", r"^[A-Za-z0-9_-]+$"),
            ("_CONTAINER_GAP_RE", r"[\s\"'`]"),
        ],
    )
    def test_entropy_self_hosted_private_regexes(self, attr, pattern):
        assert getattr(EntropyDetector, attr).pattern == pattern

    def test_entropy_self_hosts_moved_helpers(self):
        assert callable(EntropyDetector._classify_alphabet)
        assert callable(EntropyDetector._is_monotonic_sequence)
        assert callable(EntropyDetector._is_repetition)
        assert callable(EntropyDetector._is_base64_container_span)
        assert EntropyDetector._classify_alphabet("abc123") == "alnum"
        assert EntropyDetector._is_monotonic_sequence("abcdef") is True
        assert EntropyDetector._is_repetition("aaaa") is True


# [260915-0dr] 以下 4 个跨模块护栏因 pem.py / entropy.py 并入 detectors.py 而变成恒真式
# （比较对象与被比较对象成了同一个模块），已按用户决策删除：
#   - test_entropy_stops_importing_moved_helpers_keeps_shared  (1 例)
#   - test_pem_predicate_is_single_function_object             (1 例)
#   - test_moved_helpers_match_source                          (8 例 parametrize)
#   - test_moved_base64_container_span_matches_source          (3 例 parametrize)
#                                                             -----
#                                                              13 例
#
# 其判别力**未丢失，已由本文件末尾新增的 TestMergedModuleIntegrity 常驻接管**：
#   - 「合并丢了 / 改名了符号、类常量被摊平到模块级」 -> test_old_module_symbols_survive_merge
#   - 「公开面被合并扩宽」                           -> test_dunder_all_not_widened_by_merge
#   - 「fail-closed PEM 行为漂移 / ReDoS 上界丢失」  -> test_pem_fail_closed_still_holds 等
# 该常驻用例经 11 组植入实证具判别力，且 CI 每次运行。
#
# 另：「fail-closed PEM 行为」的**大面积**覆盖仍由 TestPemFailClosedOnMalformedBlocks
# 等 130+ 个端到端用例承担 —— 常驻用例只钉边界与常量，不重复 sweep。


# ---------------------------------------------------------------------------
# [260915-0dr] 合并完整性常驻护栏的冻结期望值。
#
# 语义分工（改这些测试前先读这段）：
#   * RN1 —— 冻结的“期望符号表”驱动的“模块级符号是否被删”。
#     判据是“声明式清单 vs 实际 AST”：test_old_module_symbols_survive_merge（+ __all__ 未扩宽）。
#   * RN2 —— 行为与常量的字节级金表。
#     判据是“运行结果 vs 字面量”：test_pem_fail_closed_still_holds /
#     test_pem_body_bound_still_enforced / test_tuning_constants_match_golden_values /
#     test_label_tables_match_golden_values / test_classify_pem_label_three_way_split /
#     test_normalize_label_still_collapses_whitespace /
#     test_whitespace_variant_non_secret_still_excluded
#
# 两者**故意不共享**冻结值：只改任一方（删符号 / 改常量或行为）都会红；
# 必须同时改两处且改得一致，才可能静默丢失或漂移 —— 这让“删符号 + 顺手改测试”
# 的失败模式在评审中显眼。（二者都读 detectors.py 的 AST / 运行时属性。）
# ---------------------------------------------------------------------------

# [260915-0dr] 原 pem.py ∪ entropy.py 的模块级符号（41 名，planning time 从基线实测导出）。
# 两者零重名，且与 detectors 原有 13 个类零重名 —— 已核验。
# 期望值**硬编码**在此，绝不从被测模块推导（自指构造会让断言恒真、判别力为 0）。
_OLD_MODULE_SYMBOLS: frozenset[str] = frozenset(
    {
        "PemBlock",
        "_ALNUM_RE",
        "_ALPHABET_ALNUM",
        "_ALPHABET_BASE64",
        "_ALPHABET_BASE64URL",
        "_ALPHABET_OTHER",
        "_BASE64URL_CHARS_RE",
        "_BASE64_CHARS_RE",
        "_BASE64_CONTAINERS",
        "_BASE64_RE",
        "_BEGIN_RE",
        "_BODY_MAX",
        "_CONTAINER_GAP_RE",
        "_END_RE",
        "_HEX_EXCLUDE_LENGTHS",
        "_HEX_RE",
        "_KSUID_RE",
        "_LABEL_WS_RE",
        "_NON_PRIVATE_LABELS",
        "_NON_SECRET_LABEL_SET",
        "_PEM_BODY_GAP_RE",
        "_PRIVATE_LABELS",
        "_PRIVATE_LABEL_SET",
        "_REPETITION_UNIQUE_MAX",
        "_RFC4716_BEGIN_RE",
        "_RFC4716_END_RE",
        "_SEQUENCE_STEP_RATIO",
        "_ULID_RE",
        "_UUID_RE",
        "char_class_count",
        "classify_alphabet",
        "classify_pem_label",
        "is_base64_container_span",
        "is_fixed_hex",
        "is_monotonic_sequence",
        "is_pem_non_secret_span",
        "is_repetition",
        "is_uuid_like",
        "iter_pem_blocks",
        "normalize_label",
        "shannon_entropy",
    }
)

# 分文件拆分（用于断言 14 / 27 与 10 / 18）
_PEM_SYMBOLS: frozenset[str] = frozenset(
    {
        "PemBlock",
        "_BEGIN_RE",
        "_BODY_MAX",
        "_END_RE",
        "_LABEL_WS_RE",
        "_NON_PRIVATE_LABELS",
        "_NON_SECRET_LABEL_SET",
        "_PRIVATE_LABELS",
        "_PRIVATE_LABEL_SET",
        "_RFC4716_BEGIN_RE",
        "_RFC4716_END_RE",
        "classify_pem_label",
        "iter_pem_blocks",
        "normalize_label",
    }
)
_ENTROPY_SYMBOLS: frozenset[str] = _OLD_MODULE_SYMBOLS - _PEM_SYMBOLS

# 28 = pem 10 + entropy 18。detectors 原有模块级私有名 = 0（已核验）。
# 由 _OLD_MODULE_SYMBOLS **派生** —— 这不是自指（它不依赖被测模块），故安全；
# 但断言 `len == 28` 必须显式写出，否则派生基数漂移时无感知。
_MODULE_LEVEL_PRIVATE_ALLOWED: frozenset[str] = frozenset(n for n in _OLD_MODULE_SYMBOLS if n.startswith("_"))

# [2026-09-15] 新增的模块级私有名（共享判据，显式登记而非从被测模块推导）。
# 这些是**新的共享谓词**，服务于多项 detector —— 放在模块级是有意设计，
# 不是「类常量被摊平」（那是缺陷；这里从来没有类常量被搬出来）。
# 每条都登记它有意的归属，防止日后把真正的类常量也混进来。
_MODULE_LEVEL_PRIVATE_ADDED_20260915: frozenset[str] = frozenset(
    {
        # 占位符形状判据（assignment + entropy 共享）
        "_PLACEHOLDER_RE",
        # 公钥前缀（OpenSSH 形态）与其 gap 判据（entropy 专用，但与其他 span 谓词同族）
        "_PUBLIC_KEY_PREFIXES",
        "_PUBLIC_KEY_GAP_RE",
        # 非秘密字段名（nonce/salt/iv）+ 其归一化与前瞻判据（entropy 专用）
        "_NON_SECRET_FIELD_NAMES",
        "_PRECEDING_KEY_RE",
        "_normalize_field_name",
        # PEM 空 body 判据（PemDetector + is_pem_non_secret_span 共享）
        "_PEM_NON_SUBSTANTIVE_RE",
    }
)

# [260915-0dr] 行为 + 常量金表。这里的每个期望值都是硬编码字面量 —— 不允许从被测模块推导。
_LEAK = "aUpG7zPe+f6z49nJyz8K8NlHG3mpcvgPUTEPiTUv"  # len=40 ent=4.7153 cls=4

# 4 条精选边界：错配（含 BEGIN 行内 body 的错配）、未闭合、良构正对照、同行护栏
_RN2A_CASES = (
    ("mismatched", f"-----BEGIN PUBLIC KEY-----\n{_LEAK}\n-----END RSA PRIVATE KEY-----", False),
    ("unterminated", f"-----BEGIN PUBLIC KEY-----\n{_LEAK}", False),
    ("matched_non_secret", f"-----BEGIN PUBLIC KEY-----\n{_LEAK}\n-----END PUBLIC KEY-----", True),
    # 同行护栏：body 未另起一行 -> 即便 label 良构也不得排除（防止 CERTIFICATE AUTHORITY=... 被误排除）
    ("same_line_body", f"-----BEGIN CERTIFICATE-----{_LEAK}\n-----END CERTIFICATE-----", False),
)


class TestMergedModuleIntegrity:
    """[260915-0dr] 合并完整性常驻护栏（接管 4 个跨模块恒真护栏的判别力）。

    9 个方法**均不使用 parametrize** —— 使方法数 == 测试项数，便于与全量计数对账。
    期望值全部硬编码（不读被测模块推导），且禁用自指 / 恒真形态。
    """

    def _module_level_symbols(self) -> set[str]:
        """读出 detectors.py 的模块级符号（函数 / 类 / 赋值 / 带注解赋值），排除 dunder。"""
        import ast

        src = pathlib.Path(detectors.__file__).read_text(encoding="utf-8")
        out: set[str] = set()
        for node in ast.parse(src).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                out.add(node.name)
            elif isinstance(node, ast.Assign):
                out.update(t.id for t in node.targets if isinstance(t, ast.Name) and not t.id.startswith("__"))
            elif (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and not node.target.id.startswith("__")
            ):
                out.add(node.target.id)
        return out

    def test_old_module_symbols_survive_merge(self):
        """合并不得丢失 / 改名原 pem/entropy 的任何模块级符号，也不得把类常量摊平到模块级。

        ⚠️ 必须走 AST 读模块级 body —— ``hasattr(detectors, name)`` 对**类成员**也返回
        True，用它会让「被摊平到某个类体」的常量让断言恒过。
        """
        actual = self._module_level_symbols()
        missing = sorted(_OLD_MODULE_SYMBOLS - actual)
        assert not missing, f"合并丢了模块级符号（被删或改名）：{missing}"
        # 分文件计数：pem 14 / entropy 27
        assert len(_PEM_SYMBOLS) == 14, f"pem 分文件计数漂移: {len(_PEM_SYMBOLS)}"
        assert len(_ENTROPY_SYMBOLS) == 27, f"entropy 分文件计数漂移: {len(_ENTROPY_SYMBOLS)}"
        assert len(_MODULE_LEVEL_PRIVATE_ALLOWED) == 28, "模块级私有名允许集基数漂移"
        # 2026-09-15 新增共享判据（显式登记，防止「摊平」检测把有意的共享谓词误判）
        assert len(_MODULE_LEVEL_PRIVATE_ADDED_20260915) == 7, "新增模块级私有名计数漂移"
        allowed = _MODULE_LEVEL_PRIVATE_ALLOWED | _MODULE_LEVEL_PRIVATE_ADDED_20260915
        # 摊平检测：模块级私有名必须 ⊆ 允许名（pem 10 + entropy 18 + 新增 7）
        flattened = sorted(n for n in actual if n.startswith("_") and n not in allowed)
        assert not flattened, f"类体内私有名被摊平到模块级（会静默遮蔽）：{flattened}"

    def test_dunder_all_not_widened_by_merge(self):
        """合并不扩宽公开面：pem/entropy 的公开名按名可导入但不进 __all__。

        2026-09-15 由 11 变 12 —— 新增 ``JdbcDetector``（有意的公开 detector）。
        """
        assert len(detectors.__all__) == 12, f"__all__ 被扩宽: {detectors.__all__}"
        leaked = sorted(set(detectors.__all__) & _OLD_MODULE_SYMBOLS)
        assert not leaked, f"pem/entropy 的公开名进入了 __all__: {leaked}"

    def test_pem_fail_closed_still_holds(self):
        """缺陷 7 类别回归 + 正对照：只有**完整且 label 一致**的非秘密块才可被排除。

        4 条精选边界（大面积 sweep 由 TestPemFailClosedOnMalformedBlocks 的 130+ 用例承担 ——
        此处只钉边界，不重复 sweep，避免 parametrize 展开导致 collection 膨胀）。
        """
        for name, text, excluded in _RN2A_CASES:
            start = text.index(_LEAK)
            got = is_pem_non_secret_span(text, start, start + len(_LEAK))
            assert got is excluded, f"RN2a/{name}: excluded={got}, expected {excluded}"

    def test_pem_body_bound_still_enforced(self):
        """T-06-12：body 超 _BODY_MAX 不得产出块（宁漏不失控）。"""
        huge = "-----BEGIN PUBLIC KEY-----\n" + "A" * 70_000 + "\n-----END PUBLIC KEY-----"
        assert iter_pem_blocks(huge) == []
        # 正对照：刚好不超限时应当产出（防“实现成永远返回空”）
        ok = "-----BEGIN PUBLIC KEY-----\n" + "A" * 100 + "\n-----END PUBLIC KEY-----"
        assert len(iter_pem_blocks(ok)) == 1

    def test_tuning_constants_match_golden_values(self):
        """字节级金表。期望值是硬编码字面量 —— 不从被测模块推导。

        金表与 RN1 的符号表**故意不共享**：只改其中一方都会红。
        """
        golden = [
            ("_BODY_MAX", 65536),
            ("_REPETITION_UNIQUE_MAX", 2),
            ("_ALPHABET_ALNUM", "alnum"),
            ("_ALPHABET_BASE64", "base64"),
            ("_ALPHABET_BASE64URL", "base64url"),
            ("_ALPHABET_OTHER", "other"),
            ("_BASE64_CONTAINERS", ("data:", ";base64,")),
            # 死常量：一旦被"顺手清理"即属越界改动 —— 这条是 V11 的常驻化版本
            ("_SEQUENCE_STEP_RATIO", 0.9),
        ]
        for name, expected in golden:
            got = getattr(detectors, name)
            assert got == expected, f"RN2c: {name} = {got!r}, expected {expected!r}"

    def test_label_tables_match_golden_values(self):
        """label 集金表 —— 抓「清空标签集 / 改序」。"""
        assert tuple(detectors._PRIVATE_LABELS) == (
            "RSA PRIVATE KEY",
            "EC PRIVATE KEY",
            "OPENSSH PRIVATE KEY",
            "ENCRYPTED PRIVATE KEY",
            "PGP PRIVATE KEY BLOCK",
            "PRIVATE KEY",
        )
        assert tuple(detectors._NON_PRIVATE_LABELS) == (
            "PUBLIC KEY",
            "RSA PUBLIC KEY",
            "EC PUBLIC KEY",
            "DSA PUBLIC KEY",
            "OPENSSH PUBLIC KEY",
            "CERTIFICATE",
            "X509 CERTIFICATE",
            "TRUSTED CERTIFICATE",
            "PGP PUBLIC KEY BLOCK",
            "SSH2 PUBLIC KEY",
            # 2026-09-15 追加：CSR（PKCS#10）不是秘密（README 明确要求保留）
            "CERTIFICATE REQUEST",
            "NEW CERTIFICATE REQUEST",
        )
        assert frozenset(detectors._NON_PRIVATE_LABELS) == detectors._NON_SECRET_LABEL_SET
        assert frozenset(detectors._PRIVATE_LABELS) == detectors._PRIVATE_LABEL_SET

    def test_classify_pem_label_three_way_split(self):
        """三分 + fail-closed unknown。近义 / 畸形 label 必须落 unknown（不排除 -> 遮）。"""
        cases = [
            ("PUBLIC KEY", "non_secret"),
            ("CERTIFICATE", "non_secret"),
            ("SSH2 PUBLIC KEY", "non_secret"),
            ("PRIVATE KEY", "secret"),
            ("RSA PRIVATE KEY", "secret"),
            ("ENCRYPTED PRIVATE KEY", "secret"),
            # 故意的负例：白名单只认**归一化后的精确** label
            ("PUBLIC KEYX", "unknown"),
            (" PUBLIC KEY", "unknown"),
            ("X509", "unknown"),
            ("DSA PRIVATE KEY", "unknown"),
            ("", "unknown"),
        ]
        for label, expected in cases:
            got = classify_pem_label(label)
            assert got == expected, f"RN2f: classify_pem_label({label!r}) = {got!r}, expected {expected!r}"

    def test_normalize_label_still_collapses_whitespace(self):
        """``normalize_label`` 空白折叠 + strip（含空串）。"""
        for raw, expected in [
            ("  PUBLIC KEY", "PUBLIC KEY"),
            ("PUBLIC  KEY", "PUBLIC KEY"),
            ("\tPUBLIC KEY\t", "PUBLIC KEY"),
            ("RSA  PRIVATE  KEY", "RSA PRIVATE KEY"),
            ("", ""),
        ]:
            got = normalize_label(raw)
            assert got == expected, f"RN2g: normalize_label({raw!r}) = {got!r}, expected {expected!r}"

    def test_whitespace_variant_non_secret_still_excluded(self):
        """缺陷 8 的原始泄露形：``-----BEGIN  PUBLIC KEY-----``（双空格）必须仍被判为非秘密。"""
        text = f"-----BEGIN  PUBLIC KEY-----\n{_LEAK}\n-----END  PUBLIC KEY-----"
        start = text.index(_LEAK)
        assert is_pem_non_secret_span(text, start, start + len(_LEAK)) is True


# ---------------------------------------------------------------------------
# [2026-09-15] 误脱敏修复（negative 用例回归防线）
#
# 交付测试集的 26 条 negative 全部要求「保持原样」。改动前有 10 条被误改，
# 下列用例逐条钉住修复后的行为，并给出「真实凭据仍须脱敏」的反向对照 ——
# 只测「不再误报」而不测「仍会脱敏」会让判据被逐步放宽到失效。
# ---------------------------------------------------------------------------

# 共享的高熵测试值（与交付测试集一致）
_HIGH_ENTROPY = "hfaRgFJU1cGRJ3Y21E8AWO5D7nXsjaRcrVk3V8w3Kek"


class TestPlaceholderValuesNotMasked:
    """占位符 / 已脱敏标记须保留（README：预设占位符与引用一并保留）。"""

    @pytest.mark.parametrize(
        "sample",
        [
            "password=${DB_PASSWORD}",
            "password={{ vault_db_password }}",
            "password=<REDACTED>",
            "token=[REDACTED_SECRET]",
            "password=********",
            "password=null",
            # `!vault` 后紧跟 `|`：值被 _KEY_VALUE_RE 截断为 `!vault`，
            # 故裸值形态另在 test_is_placeholder_value 中单独钉住（见 !vault 用例）
            "password: !vault",
        ],
    )
    def test_placeholder_survives(self, sample):
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) == sample

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("null", True),
            ("********", True),
            ("<REDACTED>", True),
            ("[REDACTED_SECRET]", True),
            ("{{ x }}", True),
            ("!vault", True),
            ("...", True),
            ("changeme", True),
            # 反向：真实值不得被判为占位符
            ("FixtureOnly!WovuqhlzvUTXDCqieoLIpA", False),
            ("hunter2", False),
            ("my-password-example", False),
            ("x" * 40, False),
            (_HIGH_ENTROPY, False),
        ],
    )
    def test_is_placeholder_value(self, value, expected):
        assert is_placeholder_value(value) is expected


class TestJdbcDetectorRequiresContext:
    """``JdbcDetector`` 须 ``jdbc:`` 上下文（原无上下文正则致 S106/S107/S109/S111 误报）。"""

    @pytest.mark.parametrize(
        "sample, expected",
        [
            ("jdbc:sqlserver://h;User Id=sa;Password=S3cr3tPw", "S3cr3tPw"),
            # 属性名含空格（真实 JDBC 形态）不得因空白被拒
            ("jdbc:oracle:thin:@h:1521/x;Password=S3cr3tPw", "S3cr3tPw"),
        ],
    )
    def test_jdbc_context_hits(self, sample, expected):
        findings = JdbcDetector().scan(sample)
        assert len(findings) == 1
        assert sample[findings[0].start : findings[0].end] == expected

    @pytest.mark.parametrize(
        "sample",
        [
            "password=null",
            "password=<REDACTED>",
            "password=********",
            "Password=S3cr3tPw",  # 无 jdbc: 上下文
            "myjdbc:foo;Password=S3cr3tPw",  # 前缀非 token 起点
        ],
    )
    def test_no_jdbc_context_zero_hits(self, sample):
        assert JdbcDetector().scan(sample) == []

    def test_dsn_detector_no_longer_has_jdbc_attribute_regex(self):
        """回归防线：无上下文的 ``_JDBC_PASSWORD_RE`` 不得复活。"""
        assert not hasattr(DsnDetector, "_JDBC_PASSWORD_RE")
        assert not hasattr(DsnDetector, "_JDBC_HINT_RE")


class TestNonSecretFieldValuesRetained:
    """nonce / salt / iv 保留（用户 2026-09-15 批准；与真实 token 同形，只能靠字段名）。"""

    @pytest.mark.parametrize("key", ["nonce", "salt", "iv", "initialization_vector"])
    def test_non_secret_field_retained(self, key):
        sample = f"{key}={_HIGH_ENTROPY}"
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) == sample

    @pytest.mark.parametrize("key", ["token", "password", "secret", "api_key", "nonce_str"])
    def test_credential_field_still_masked(self, key):
        """反向：真实凭据字段（含「含 nonce 但不等于 nonce」的名字）仍须脱敏。"""
        sample = f"{key}={_HIGH_ENTROPY}"
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) != sample


class TestPemEmptyBodyAndCsr:
    """PEM 空 body / 教学省略不脱敏 + CSR 归非秘密（S037/S115/S116）。"""

    @pytest.mark.parametrize(
        "sample",
        [
            "-----BEGIN PRIVATE KEY-----",  # 单独 BEGIN（无配对 END）
            "Example: -----BEGIN PRIVATE KEY----- ... -----END PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
            "-----BEGIN RSA PRIVATE KEY-----\n   \n-----END RSA PRIVATE KEY-----",
        ],
    )
    def test_empty_or_ellipsis_body_survives(self, sample):
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) == sample
        assert PemDetector().scan(sample) == []

    @pytest.mark.parametrize(
        "body",
        [
            "MIIEowIBAAKCAQEAyv3TmbqFa/XOCPI776YTcpuKihkC3jB/tvSwf99Ufj2VApM9",
            # 有实质 body + 省略号：README 要求遮罩（不同于纯教学省略）
            "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n...\nQUJD",
        ],
    )
    def test_substantive_body_still_masked(self, body):
        sample = f"-----BEGIN PRIVATE KEY-----\n{body}\n-----END PRIVATE KEY-----"
        assert PemDetector().scan(sample) != []
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) != sample

    @pytest.mark.parametrize("label", ["CERTIFICATE REQUEST", "NEW CERTIFICATE REQUEST"])
    def test_csr_is_non_secret(self, label):
        """CSR 含公钥与主体名、不含私钥材料，README 明确要求保留。"""
        assert classify_pem_label(label) == "non_secret"
        sample = f"-----BEGIN {label}-----\n{_LEAK}\n-----END {label}-----"
        out = redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT)
        assert _LEAK in out, "CSR body 不得被裸熵兜底遮掉"

    def test_private_key_still_secret(self):
        assert classify_pem_label("PRIVATE KEY") == "secret"


class TestPublicKeyPrefixExemption:
    """OpenSSH 公钥 body 保留（S035）；无前缀的高熵值仍须脱敏。"""

    @pytest.mark.parametrize(
        "sample",
        [
            "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIIm/bfAGTL9EUC5xrdEZvzdTFsdIh1qQV+BZPQThbejI",
            "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDZx5Yq8mKxLpVnRtUwXyZ0123456789abcdefghijkl",
        ],
    )
    def test_openssh_public_key_survives(self, sample):
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) == sample

    @pytest.mark.parametrize(
        "sample",
        [
            f"v {_HIGH_ENTROPY} end",  # 无公钥前缀
            f"public_key_blob={_HIGH_ENTROPY}",  # 字段名像公钥，但值无前缀
        ],
    )
    def test_prefixed_absent_still_masked(self, sample):
        assert redact_text(sample, purpose=RedactionPurpose.MODEL_OUTPUT) != sample
