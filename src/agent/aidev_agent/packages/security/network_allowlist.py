# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.

网络/域名白名单防护。

从工具调用参数中提取主机名/域名，并按白名单（``AIDEV_ALLOW_DOMAINS``）与
黑名单（``AIDEV_BLOCK_DOMAINS``）判定是否放行，阻断数据外泄到未授权域名。

对齐 OpenClaw ``allow_domains`` / Hermes ``network allowlist`` 语义：

- 白名单为空 → fail-open（不限制，仅黑名单 / 私网地址生效）；
- 白名单非空 → 仅放行命中白名单的域名；
- 私网 / 回环地址一律阻断（SSRF 防护，不受白名单影响）。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# 主机提取：http(s)://host、裸域名、IPv4、IPv6（方括号）
# 前后负向断言避免截取片段（如 example.com 在 example.com.au 中误截）
_HOST_RE = re.compile(
    r"(?<![\w.-])"
    r"(?:https?://)?"
    r"(?:"
    r"[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    r"(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?)+"
    r"|localhost"
    r"|\d{1,3}(?:\.\d{1,3}){3}"
    r"|\[[0-9a-fA-F:]+\]"
    r")"
    r"(?![\w.-])",
)

# 私网 / 回环前缀（IPv4 私网段、链路本地、环回、0.x、172.16.0.0/12）
_PRIVATE_PREFIXES = (
    "10.",
    "127.",
    "192.168.",
    "169.254.",
    "0.",
    *(f"172.{i}." for i in range(16, 32)),
)


def extract_hosts(text: Any) -> list[str]:
    """从文本中提取主机名 / 域名 / IP（小写、去尾点、去 IPv6 方括号）。"""
    if not isinstance(text, str) or not text:
        return []
    hosts: list[str] = []
    for m in _HOST_RE.finditer(text):
        host = m.group(0)
        # 去掉 scheme 前缀，只保留 host 部分
        if host.startswith(("http://", "https://")):
            host = host.split("://", 1)[1]
        if host:
            hosts.append(host.strip("[]").lower().rstrip("."))
    return hosts


def _is_private(host: str) -> bool:
    """判断是否私网 / 回环地址（SSRF 风险，一律阻断）。"""
    if host in {"localhost", "::1"}:
        return True
    return any(host.startswith(p) for p in _PRIVATE_PREFIXES)


def _match(host: str, pattern: str) -> bool:
    """域名匹配（支持 ``*`` 全通配与 ``*.example.com`` 前缀通配）。"""
    pattern = (pattern or "").strip().lower().rstrip(".")
    host = (host or "").strip().lower().rstrip(".")
    if not pattern:
        return False
    if pattern == "*":
        return True
    if pattern.startswith("*."):
        suffix = pattern[2:]
        return host == suffix or host.endswith("." + suffix)
    return host == pattern


def host_is_blocked(host: str, block: Iterable[str]) -> bool:
    return any(_match(host, p) for p in block)


def host_is_allowed(host: str, allow: Iterable[str]) -> bool:
    allow = list(allow)
    if not allow:
        return True  # fail-open
    return any(_match(host, p) for p in allow)


def evaluate_host(host: str, allow: Iterable[str], block: Iterable[str]) -> bool:
    """判定单个 host 是否放行：私网优先阻断 → 黑名单 → 白名单。"""
    if _is_private(host):
        return False
    if host_is_blocked(host, block):
        return False
    return host_is_allowed(host, allow)


def evaluate_hosts(hosts: Iterable[str], allow: Iterable[str], block: Iterable[str]) -> tuple[bool, list[str]]:
    """批量判定：返回 ``(是否全部放行, 被阻断的 host 列表)``。"""
    blocked = [h for h in hosts if not evaluate_host(h, allow, block)]
    return (not blocked, blocked)


def find_blocked_hosts(
    hosts: Iterable[str],
    allow: Iterable[str],
    block: Iterable[str],
) -> list[str]:
    """返回会被网络白名单阻断的 host（私网 / 黑名单 / 未授权域名）。

    ``allow`` / ``block`` 必须由调用方显式传入（来自 ``SecuritySettings``），
    本模块不再读取环境变量 —— 安全配置的唯一入口是 ``AgentConfig.security_settings``。
    """
    return [h for h in hosts if not evaluate_host(h, allow, block)]


__all__ = [
    "extract_hosts",
    "host_is_blocked",
    "host_is_allowed",
    "evaluate_host",
    "evaluate_hosts",
    "find_blocked_hosts",
]
