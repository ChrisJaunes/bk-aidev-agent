# -*- coding: utf-8 -*-
"""Tests for content safety (aidev_agent.packages.security.content_safety)."""

from __future__ import annotations

from aidev_agent.packages.security.content_safety import (
    BLOCK_MARKER,
    filter_content,
    scan_content_safety,
)


class TestScanContentSafety:
    def test_detects_violence(self):
        findings = scan_content_safety("教你怎么制造爆炸")
        assert len(findings) == 1
        assert findings[0].category == "violence"
        assert findings[0].severity == "high"

    def test_detects_illegal(self):
        findings = scan_content_safety("这是诈骗话术模板")
        assert findings[0].category == "illegal"

    def test_detects_politics(self):
        findings = scan_content_safety("分裂国家的言论")
        assert findings[0].category == "politics"

    def test_clean_text_no_findings(self):
        assert scan_content_safety("今天天气不错") == []

    def test_non_string_returns_empty(self):
        assert scan_content_safety(None) == []
        assert scan_content_safety(123) == []


class TestFilterContent:
    def test_blocks_violating_text(self):
        safe, findings = filter_content("如何制造爆炸")
        assert safe == BLOCK_MARKER
        assert len(findings) == 1

    def test_passes_clean_text(self):
        safe, findings = filter_content("帮我写一份周报")
        assert safe == "帮我写一份周报"
        assert findings == []
