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

Reward Hacking 防护。

Reward hacking（奖励黑客 / 回报操控）：模型为达成一个已被拦截的目标，反复换用
不同工具、改写措辞、拆分步骤来绕过安全边界。本模块负责：

1. 危险意图识别：基于威胁模式（外泄 / 后门）+ 危险动作关键词，判断本次工具
   调用是否表达了一个会被拦截的危险意图；
2. 意图指纹：把意图的「具体目标」（域名 / IP / 文件路径）归一化为指纹，使
   「换说法」的同一目标收敛到同一指纹；
3. 会话级台账：按 thread_id 记录每个指纹的尝试次数，检测重复尝试并升级处置。

本模块只负责「检测 + 升级」，不负责首次拦截——首次危险动作由命令白名单 /
文件敏感路径 / 网络白名单等硬约束拦截；当模型在拦截后仍反复尝试，由本模块
识别为 reward hacking 并升级阻断。
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from aidev_agent.packages.security.dangerous_patterns import REWARD_PATTERNS
from aidev_agent.packages.security.network_allowlist import extract_hosts, find_blocked_hosts
from aidev_agent.packages.security.threat_patterns import scan_for_threats

logger = logging.getLogger(__name__)

# 危险动作由 ``packages.security.dangerous_patterns.REWARD_PATTERNS`` 单一数据源提供
# （破坏性 / 提权 / 远程执行管道绕过类），此处不再重复维护正则表。

# 文件路径提取（绝对路径 / 相对路径片段）
_PATH_RE = re.compile(r"/[^\s\"'`,;]+")

_MAX_LEDGER_ENTRIES = 512
_DEFAULT_KEY = "__default__"


def _flatten(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return " ".join(_flatten(v) for v in obj.values())
    if isinstance(obj, (list, tuple, set)):
        return " ".join(_flatten(v) for v in obj)
    return str(obj) if obj is not None else ""


def detect_dangerous_intent(
    tool_name: str,
    args: Any,
    allow: list[str] | None = None,
    block: list[str] | None = None,
) -> list[str]:
    """返回危险意图命中项（威胁模式名 / 危险动作标签），空列表表示无危险意图。

    ``allow`` / ``block`` 为网络白名单 / 黑名单（来自 ``SecuritySettings`` 拆解，
    由调用方显式传入）；为 ``None`` 时视为「未配置」，即空列表。
    """
    text = f"{tool_name} {_flatten(args)}"
    findings: list[str] = [f.pattern_name for f in scan_for_threats(text, scope="strict")]
    for pattern in REWARD_PATTERNS:
        if pattern.pattern.search(text):
            findings.append(pattern.label)
    # 外泄 / SSRF 意图：目标域名会被网络白名单阻断（私网 / 黑名单 / 未授权）
    # allow / block 为 None 表示「未配置」，等价于空列表（本模块不读 env）
    blocked = find_blocked_hosts(extract_hosts(text), allow=allow or [], block=block or [])
    if blocked:
        findings.append("network_blocked_target")
    return findings


def intent_fingerprint(tool_name: str, args: Any, findings: list[str]) -> str:
    """对危险意图做指纹：优先用具体目标（域名 / IP / 路径），无目标回退到类别。"""
    text = _flatten(args)
    hosts = extract_hosts(text)
    paths = _PATH_RE.findall(text)
    targets = sorted(set(hosts + paths))
    if targets:
        return "targets:" + "|".join(targets)
    return "category:" + (findings[0] if findings else tool_name)


@dataclass
class RewardHackingLedger:
    """会话级危险意图台账（thread_id 维度，有界 LRU）。

    记录每个指纹的尝试次数与最近一次工具名，用于识别「换说法重试」的
    reward hacking 行为。进程内共享，跨 wrapper 调用保持一致。
    """

    _lock: Lock = field(default_factory=Lock, repr=False)
    _sessions: "OrderedDict[str, OrderedDict[str, dict[str, Any]]]" = field(default_factory=OrderedDict, repr=False)

    def record(self, key: str, fingerprint: str, tool_name: str) -> int:
        """记录一次危险意图尝试，返回该指纹累计次数（本次记入后）。"""
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                session = OrderedDict()
                self._sessions[key] = session
                while len(self._sessions) > _MAX_LEDGER_ENTRIES:
                    self._sessions.popitem(last=False)

            entry = session.get(fingerprint)
            if entry is None:
                entry = {"count": 0, "tool": tool_name}
                session[fingerprint] = entry
                while len(session) > _MAX_LEDGER_ENTRIES:
                    session.popitem(last=False)
            entry["count"] += 1
            entry["tool"] = tool_name
            return entry["count"]


# 进程级单例台账（跨 wrapper 调用共享）
_ledger = RewardHackingLedger()


def thread_key(runtime: Any) -> str:
    """从 request.runtime 提取 thread_id 作为会话键，无 runtime 时回退全局键。"""
    try:
        config = getattr(runtime, "config", None)
        if config:
            return str(config.get("configurable", {}).get("thread_id") or _DEFAULT_KEY)
    except Exception:  # noqa: BLE001
        pass
    return _DEFAULT_KEY


__all__ = [
    "detect_dangerous_intent",
    "intent_fingerprint",
    "RewardHackingLedger",
    "_ledger",
    "thread_key",
]
