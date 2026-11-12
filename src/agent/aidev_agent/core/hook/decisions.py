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

from dataclasses import dataclass
from enum import Enum
from typing import Any

__all__ = ["HookDecisionKind", "HookDecision", "PASS"]


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
