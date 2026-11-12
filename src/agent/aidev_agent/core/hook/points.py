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

from enum import Enum

__all__ = ["HookPoint", "ALLOWED_DECISION_KINDS"]


class HookPoint(str, Enum):
    """本期固定的 5 个 hook 点位；取值即点位名，不得改名。"""

    SESSION_START = "session_start"
    MODEL_PRE = "model_pre"
    MODEL_POST = "model_post"
    TOOL_PRE = "tool_pre"
    TOOL_POST = "tool_post"


# 各点位允许的裁决取值（按点位名登记，避免与 decisions 模块循环引用）。
# 表中未登记的取值一律视为非法裁决，由 runner 按 pass 降级并记 warning。
ALLOWED_DECISION_KINDS: dict[HookPoint, frozenset[str]] = {
    HookPoint.SESSION_START: frozenset({"pass", "deny", "append"}),
    HookPoint.MODEL_PRE: frozenset({"pass", "deny", "replace"}),
    HookPoint.MODEL_POST: frozenset({"pass", "deny", "replace"}),
    HookPoint.TOOL_PRE: frozenset({"pass", "deny", "ask", "replace"}),
    HookPoint.TOOL_POST: frozenset({"pass", "deny", "replace", "append"}),
}
