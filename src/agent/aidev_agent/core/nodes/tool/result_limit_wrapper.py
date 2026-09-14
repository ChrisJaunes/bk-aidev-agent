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

工具结果长度限制（最小化截断）。

当工具返回内容超过 ``result_limit`` 阈值时，不再整段替换为「超长」提示，而是
对字符串内容做「保头保尾」截断（``truncate_minimized``），保留关键信息、丢弃
中间冗余，最小化信息损失（对齐 OpenClaw / Hermes 的 minimized truncation）。

结构化内容（list / dict 等）无法安全截断，回退为拒绝消息。
"""

from __future__ import annotations

from collections.abc import Awaitable
from functools import lru_cache
from typing import Callable

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import AsyncToolCallWrapper, ToolCallRequest, ToolCallWrapper
from langgraph.types import Command

from aidev_agent.packages.security.truncation import truncate_minimized

TOOL_RESULT_TOO_LONG_MESSAGE = "本次工具调用返回结果超长，请重新调整调用参数"


def _tool_msg_content_len(msg: ToolMessage) -> int:
    """计算 ToolMessage 内容的字符串长度。"""
    content = getattr(msg, "content", None)
    if content is None:
        return 0
    return len(str(content))


def _truncate_msg(msg: ToolMessage, result_limit: int, keep_head: int | None, keep_tail: int | None) -> None:
    """对超长 ToolMessage 做最小化截断（str 内容保头保尾，结构化内容回退拒绝）。"""
    content = msg.content
    if isinstance(content, str):
        msg.content = truncate_minimized(content, result_limit, keep_head=keep_head, keep_tail=keep_tail)
        return
    # 结构化内容无法安全截断，回退为拒绝消息
    msg.content = TOOL_RESULT_TOO_LONG_MESSAGE
    msg.status = "error"


@lru_cache(maxsize=16)
def build_result_limit_sync_wrapper(
    result_limit: int,
    keep_head: int | None = None,
    keep_tail: int | None = None,
) -> ToolCallWrapper:
    """构建同步结果长度限制包装器（最小化截断）。

    Args:
        result_limit: 结果内容的最大字符数阈值。
        keep_head: 截断时头部保留字符数（None 自动取 80%）。
        keep_tail: 截断时尾部保留字符数（None 自动取 20%）。

    Returns:
        配置好的同步工具调用包装器。
    """

    def wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        msg = execute(request)
        if isinstance(msg, ToolMessage) and _tool_msg_content_len(msg) > result_limit:
            _truncate_msg(msg, result_limit, keep_head, keep_tail)
        return msg

    return wrapper


@lru_cache(maxsize=16)
def build_result_limit_async_wrapper(
    result_limit: int,
    keep_head: int | None = None,
    keep_tail: int | None = None,
) -> AsyncToolCallWrapper:
    """构建异步结果长度限制包装器（最小化截断）。"""

    async def wrapper(
        request: ToolCallRequest,
        execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        msg = await execute(request)
        if isinstance(msg, ToolMessage) and _tool_msg_content_len(msg) > result_limit:
            _truncate_msg(msg, result_limit, keep_head, keep_tail)
        return msg

    return wrapper
