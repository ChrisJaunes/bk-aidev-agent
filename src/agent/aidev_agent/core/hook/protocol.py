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

hook 协议词汇表：点位、裁决、处理器上下文与拒绝错误。

处理器作者只需要读这一个模块就能拿到全部契约类型；执行器、运行态与节点接入分别见
``runner`` / ``run_state`` / ``stages``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aidev_agent.exceptions import AgentException

__all__ = [
    "ALLOWED_DECISION_KINDS",
    "PASS",
    "HookContext",
    "HookDecision",
    "HookDecisionKind",
    "HookDeniedError",
    "HookIdentity",
    "HookPoint",
    "ModelPostPayload",
    "ModelPrePayload",
    "SessionStartPayload",
    "ToolPostPayload",
    "ToolPrePayload",
]


class HookPoint(str, Enum):
    """本期固定的 5 个 hook 点位；取值即点位名，不得改名。"""

    SESSION_START = "session_start"
    MODEL_PRE = "model_pre"
    MODEL_POST = "model_post"
    TOOL_PRE = "tool_pre"
    TOOL_POST = "tool_post"


# 各点位允许的裁决取值；表中未登记的取值一律视为非法裁决，由 runner 按 pass 降级并记 warning。
ALLOWED_DECISION_KINDS: dict[HookPoint, frozenset[str]] = {
    HookPoint.SESSION_START: frozenset({"pass", "deny", "append"}),
    HookPoint.MODEL_PRE: frozenset({"pass", "deny", "replace"}),
    HookPoint.MODEL_POST: frozenset({"pass", "deny", "replace"}),
    HookPoint.TOOL_PRE: frozenset({"pass", "deny", "ask", "replace"}),
    HookPoint.TOOL_POST: frozenset({"pass", "deny", "replace", "append"}),
}


class HookDecisionKind(str, Enum):
    """处理器可返回的裁决类型。"""

    PASS = "pass"
    DENY = "deny"
    ASK = "ask"
    REPLACE = "replace"
    APPEND = "append"


@dataclass(frozen=True)
class HookDecision:
    """单个处理器给出的裁决。

    Attributes:
        kind: 裁决类型；``None`` 返回值等价于 ``pass``。
        reason: ``deny`` / ``ask`` 必填的非空原因。
        payload: ``replace`` 的替换载荷 / ``append`` 的追加内容，必填。
    """

    kind: HookDecisionKind = HookDecisionKind.PASS
    reason: str = ""
    payload: Any = None


PASS = HookDecision()


@dataclass(frozen=True)
class HookIdentity:
    """处理器可见的只读运行标识。"""

    agent_code: str = ""
    session_code: str = ""
    run_id: str = ""
    turn: int = 1


@dataclass(frozen=True)
class SessionStartPayload:
    """``session_start`` 点位上下文。"""

    message_count: int = 0
    last_user_input: str = ""
    tool_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelPrePayload:
    """``model_pre`` 点位上下文。"""

    messages: list[Any] = field(default_factory=list)
    model_name: str = ""
    tool_names: tuple[str, ...] = ()
    turn: int = 1


@dataclass(frozen=True)
class ModelPostPayload:
    """``model_post`` 点位上下文。"""

    response: Any = None
    model_request_count: int = 0
    turn: int = 1


@dataclass(frozen=True)
class ToolPrePayload:
    """``tool_pre`` 点位上下文。"""

    tool_name: str = ""
    tool_call_id: str = ""
    arguments: dict = field(default_factory=dict)
    turn: int = 1


@dataclass(frozen=True)
class ToolPostPayload:
    """``tool_post`` 点位上下文。"""

    tool_name: str = ""
    tool_call_id: str = ""
    content: Any = ""
    is_error: bool = False
    duration_ms: int = 0
    turn: int = 1


@dataclass(frozen=True)
class HookContext:
    """传给处理器的唯一入参。"""

    point: HookPoint
    identity: HookIdentity
    payload: Any = None


class HookDeniedError(AgentException):
    """hook 返回 ``deny`` 时可读的终止错误。

    消息固定形如 ``hook deny at <point>: <reason>``，供运行入口直接呈现给调用方。
    """

    def __init__(self, point: HookPoint, reason: str = ""):
        self.point = point
        self.reason = reason
        super().__init__(message=f"hook deny at {point.value}: {reason}")
