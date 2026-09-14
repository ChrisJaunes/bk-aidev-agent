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

内容安全防护。

对模型产出文本做违规内容检测（暴恐 / 色情 / 政治敏感 / 违法），命中即返回
阻断标记，由调用方替换为安全兜底回复。

本地实现为确定性关键词基线（保守、只收录无歧义的类别指示词），并预留可插拔的
远程内容安全服务接口（默认关闭），生产环境可接入企业信安 / 天御等专业服务
替换本地基线。与 ``threat_patterns``（注入 / 外泄检测）互补：本模块聚焦
「违规内容」，前者聚焦「注入与数据外泄」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 阻断标记（替换命中内容）
BLOCK_MARKER = "[BLOCKED: 内容触发安全策略，已拦截]"

# 违规分类与保守关键词基线（仅收录无歧义的类别指示词，生产请接入专业信安服务）
_CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "violence": ("炸弹制作", "制造爆炸", "恐怖袭击", "杀人方法", "组织暴乱"),
    "pornography": ("色情服务", "淫秽内容", "裸体照片", "招嫖"),
    "illegal": ("贩卖毒品", "洗钱教程", "诈骗话术", "倒卖枪支", "伪造货币"),
    "politics": ("颠覆国家政权", "分裂国家", "煽动颠覆", "危害国家安全"),
}


@dataclass(frozen=True)
class ContentSafetyFinding:
    """单条违规内容命中记录。"""

    category: str
    keyword: str
    severity: str  # high


def scan_content_safety(text: Any) -> list[ContentSafetyFinding]:
    """扫描文本中的违规内容，返回命中记录列表。"""
    if not isinstance(text, str) or not text:
        return []
    findings: list[ContentSafetyFinding] = []
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                findings.append(ContentSafetyFinding(category=category, keyword=kw, severity="high"))
    return findings


def filter_content(text: Any) -> tuple[str, list[ContentSafetyFinding]]:
    """扫描并返回 ``(净化后的文本, 命中记录)``；命中时文本替换为阻断标记。"""
    findings = scan_content_safety(text)
    if findings:
        return BLOCK_MARKER, findings
    return text, []


__all__ = [
    "BLOCK_MARKER",
    "ContentSafetyFinding",
    "scan_content_safety",
    "filter_content",
]
