# -*- coding: utf-8 -*-
"""Tests for network/domain allowlist utilities (aidev_agent.packages.security.network_allowlist)."""

from __future__ import annotations

from aidev_agent.packages.security.network_allowlist import (
    evaluate_host,
    evaluate_hosts,
    extract_hosts,
    find_blocked_hosts,
    host_is_allowed,
    host_is_blocked,
)


class TestExtractHosts:
    def test_extracts_urls_and_bare_domains(self):
        hosts = extract_hosts("visit https://example.com and http://foo.bar/path then bare.io")
        assert "example.com" in hosts
        assert "foo.bar" in hosts
        assert "bare.io" in hosts

    def test_extracts_ipv4_and_ipv6(self):
        assert "1.2.3.4" in extract_hosts("ping 1.2.3.4")
        assert "2001:db8::1" in extract_hosts("curl http://[2001:db8::1]/x")

    def test_returns_empty_for_non_string(self):
        assert extract_hosts("") == []
        assert extract_hosts(None) == []
        assert extract_hosts(123) == []


class TestEvaluateHost:
    def test_allowlist_match(self):
        assert evaluate_host("good.com", ["good.com"], []) is True

    def test_allowlist_miss(self):
        assert evaluate_host("evil.com", ["good.com"], []) is False

    def test_fail_open_when_allow_empty(self):
        assert evaluate_host("anything.com", [], []) is True

    def test_block_wins_over_allow(self):
        assert evaluate_host("good.com", ["good.com"], ["good.com"]) is False

    def test_wildcard_prefix(self):
        assert evaluate_host("a.example.com", ["*.example.com"], []) is True
        assert evaluate_host("example.com", ["*.example.com"], []) is True
        assert evaluate_host("example.org", ["*.example.com"], []) is False

    def test_wildcard_star(self):
        assert evaluate_host("any.com", ["*"], []) is True

    def test_private_ip_always_blocked_even_if_allow_star(self):
        for host in ("10.0.0.1", "192.168.1.1", "172.16.5.5", "127.0.0.1", "localhost"):
            assert evaluate_host(host, ["*"], []) is False, host


class TestEvaluateHosts:
    def test_returns_blocked_list(self):
        allowed, blocked = evaluate_hosts(["good.com", "evil.com"], ["good.com"], [])
        assert allowed is False
        assert blocked == ["evil.com"]

    def test_all_allowed(self):
        allowed, blocked = evaluate_hosts(["good.com", "ok.com"], ["good.com", "ok.com"], [])
        assert allowed is True
        assert blocked == []


class TestFindBlockedHosts:
    def test_explicit_lists(self):
        blocked = find_blocked_hosts(["good.com", "evil.com"], ["good.com"], [])
        assert blocked == ["evil.com"]

    def test_private_ips_flagged(self):
        blocked = find_blocked_hosts(["10.0.0.1", "public.com"], [], [])
        assert blocked == ["10.0.0.1"]


class TestHostPredicates:
    def test_host_is_blocked(self):
        assert host_is_blocked("evil.com", ["evil.com"]) is True
        assert host_is_blocked("good.com", ["evil.com"]) is False

    def test_host_is_allowed(self):
        assert host_is_allowed("good.com", []) is True  # fail-open
        assert host_is_allowed("good.com", ["good.com"]) is True
        assert host_is_allowed("evil.com", ["good.com"]) is False
