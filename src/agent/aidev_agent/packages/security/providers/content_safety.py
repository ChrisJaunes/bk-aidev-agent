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

内容安全 hook（复用安平内容安全服务的外置接入点）。

本地实现为确定性关键词基线（保守），并预留可插拔的远程内容安全服务接口
（:class:`HttpContentSafetyProvider`，默认关闭）——生产环境可对接企业信安 /
天御等安平现成服务，替换本地基线。与 ``utils/content_safety`` 分工：本模块
是「外置 hook」形态（实现 :class:`SecurityHook` 协议），后者是底层扫描函数。
"""

from __future__ import annotations

import logging
from typing import Protocol

from aidev_agent.packages.security.content_safety import ContentSafetyFinding, scan_content_safety
from aidev_agent.packages.security.events import (
    SecurityEvent,
    SecurityStage,
    SecurityVerdict,
)

logger = logging.getLogger(__name__)

# 内容类检查点（命令执行有专门的白名单 / 黑名单，不在此列）
_CONTENT_STAGES = (
    SecurityStage.USER_INPUT,
    SecurityStage.TOOL_RESULT,
    SecurityStage.MODEL_OUTPUT,
    SecurityStage.FILE_SEND,
)


class RemoteContentSafetyProvider(Protocol):
    """远程内容安全服务接口（安平信安 / 天御等）。"""

    def check(self, text: str) -> list[ContentSafetyFinding]: ...


class HttpContentSafetyProvider:
    """安平内容安全服务 HTTP provider。

    通过 ``POST`` 调用远程信安服务，请求体 ``{"text": ...}``，期望响应：:

        {"blocked": false, "findings": [{"category": "...", "keyword": "...", "severity": "high"}]}

    对接具体安平服务时按该契约做薄适配层（改写请求 / 响应解析即可）。远程服务
    故障（网络 / 超时 / 非 2xx）时返回空列表（fail-open），由本地关键词基线兜底，
    避免可用性问题。
    """

    def __init__(self, endpoint: str, *, headers: dict | None = None, timeout: float = 3.0):
        if not endpoint:
            raise ValueError("远程内容安全服务 endpoint 不能为空")
        self._endpoint = endpoint
        self._headers = headers or {}
        self._timeout = timeout

    def check(self, text: str) -> list[ContentSafetyFinding]:
        if not text:
            return []
        import requests

        try:
            resp = requests.post(
                self._endpoint,
                json={"text": text},
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:  # noqa: BLE001 —— 远程服务故障 fail-open，本地基线兜底
            logger.warning("远程内容安全服务调用失败 endpoint=%s", self._endpoint, exc_info=True)
            return []

        findings: list[ContentSafetyFinding] = []
        for item in (data or {}).get("findings") or []:
            if isinstance(item, dict) and item.get("category"):
                findings.append(
                    ContentSafetyFinding(
                        category=str(item["category"]),
                        keyword=str(item.get("keyword", "")),
                        severity=str(item.get("severity", "high")),
                    )
                )
        return findings


class ContentSafetyHook:
    """内容安全 hook：本地关键词基线 + 可选远程信安服务。

    命中违规内容即返回 ``block`` 判定（fail-closed）。远程服务仅在本地基线
    未命中时作为二次确认调用，避免每请求都打远程（省时 / 降本）。
    """

    name = "content_safety"

    def __init__(self, remote: RemoteContentSafetyProvider | None = None):
        self._remote = remote

    def inspect(self, event: SecurityEvent) -> SecurityVerdict | None:
        if event.stage not in _CONTENT_STAGES:
            return None
        findings = scan_content_safety(event.content)
        if not findings and self._remote is not None:
            findings = self._remote.check(event.content)
        if not findings:
            return None
        return SecurityVerdict(
            action="block",
            hook=self.name,
            reason="内容触发安全策略，已拦截",
            findings=findings,
        )


# 默认单例：纯本地基线（远程 provider 由装配层经 build_content_safety_hook 注入）
content_safety_hook = ContentSafetyHook()


def build_content_safety_hook(remote: RemoteContentSafetyProvider | None = None) -> ContentSafetyHook:
    """构造内容安全 hook；``remote`` 为安平远程服务实现（默认 None 走纯本地基线）。"""
    return ContentSafetyHook(remote=remote)


__all__ = [
    "RemoteContentSafetyProvider",
    "HttpContentSafetyProvider",
    "ContentSafetyHook",
    "content_safety_hook",
    "build_content_safety_hook",
]
