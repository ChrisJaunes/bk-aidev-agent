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

最小化截断工具。

对超长工具结果做「保头保尾」截断：保留开头（通常含最相关信息）与结尾，
丢弃中间冗余，最小化信息损失（对齐 OpenClaw / Hermes 的 minimized truncation）。

相比整段替换为「超长」提示，保头保尾让模型仍能利用结果中的关键信息。
"""

from __future__ import annotations

from typing import Any

_MARKER_TMPL = "\n\n...[内容过长，中间已截断 {dropped} 字符]...\n\n"


def truncate_minimized(
    text: Any,
    max_chars: int,
    *,
    keep_head: int | None = None,
    keep_tail: int | None = None,
) -> str:
    """将文本截断到不超过 ``max_chars``，保留头尾、丢弃中间。

    Args:
        text: 原始文本（非 str 会先转 str）。
        max_chars: 目标总字符上限。
        keep_head: 头部保留字符数；None 时取 ``max_chars`` 的 80%。
        keep_tail: 尾部保留字符数；None 时取 ``max_chars`` 的 20%。

    Returns:
        未超长时原样返回；超长时返回「头 + 截断标记 + 尾」。
    """
    if not isinstance(text, str):
        text = str(text)
    if len(text) <= max_chars:
        return text

    if keep_head is None:
        keep_head = int(max_chars * 0.8)
    if keep_tail is None:
        keep_tail = max_chars - keep_head

    keep_head = max(0, int(keep_head))
    keep_tail = max(0, int(keep_tail))

    head = text[:keep_head]
    tail = text[-keep_tail:] if keep_tail > 0 else ""
    dropped = max(0, len(text) - keep_head - keep_tail)
    return head + _MARKER_TMPL.format(dropped=dropped) + tail


__all__ = ["truncate_minimized"]
