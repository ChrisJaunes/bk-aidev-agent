# -*- coding: utf-8 -*-
"""Tests for minimized truncation (aidev_agent.packages.security.truncation)."""

from __future__ import annotations

from aidev_agent.packages.security.truncation import truncate_minimized


class TestTruncateMinimized:
    def test_noop_under_limit(self):
        assert truncate_minimized("hello world", 100) == "hello world"

    def test_noop_at_exact_limit(self):
        text = "a" * 50
        assert truncate_minimized(text, 50) == text

    def test_keeps_head_and_tail_drops_middle(self):
        text = "HEAD-" + "M" * 1000 + "-TAIL"
        out = truncate_minimized(text, 100)
        assert out.startswith("HEAD-")
        assert out.endswith("-TAIL")
        # 中间大量 M 被丢弃，仅保留头尾
        assert out.count("M") < 1000
        assert "截断" in out

    def test_marker_present_when_truncated(self):
        out = truncate_minimized("x" * 1000, 50)
        assert "截断" in out

    def test_non_str_input_coerced(self):
        out = truncate_minimized(1234567890, 5)
        assert isinstance(out, str)
        assert "截断" in out

    def test_explicit_keep_head_tail(self):
        text = "ABCDEFGHIJ"  # 10 chars, keep head 4 + tail 3
        out = truncate_minimized(text, 7, keep_head=4, keep_tail=3)
        assert out.startswith("ABCD")
        assert out.endswith("HIJ")
