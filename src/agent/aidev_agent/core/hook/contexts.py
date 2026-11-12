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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .points import HookPoint

__all__ = [
    "HookIdentity",
    "SessionStartPayload",
    "ModelPrePayload",
    "ModelPostPayload",
    "ToolPrePayload",
    "ToolPostPayload",
    "HookContext",
]


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
