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

Agent Core hook 协议与注入入口。

对外类型：点位、裁决、上下文、处理器集合、执行器与运行态绑定。具体点位阶段函数见
``stages``，由模型节点、pv 节点与工具 wrapper 在各自位置调用。
"""

from __future__ import annotations

from .contexts import (
    HookContext,
    HookIdentity,
    ModelPostPayload,
    ModelPrePayload,
    SessionStartPayload,
    ToolPostPayload,
    ToolPrePayload,
)
from .decisions import PASS, HookDecision, HookDecisionKind
from .errors import HookDeniedError
from .points import ALLOWED_DECISION_KINDS, HookPoint
from .registry import HookHandler, HookSet
from .run_state import HookRunState, bind_run_state, finalize_run, get_run_state, record_model_response
from .runner import HookRunner

__all__ = [
    "ALLOWED_DECISION_KINDS",
    "PASS",
    "HookContext",
    "HookDecision",
    "HookDecisionKind",
    "HookDeniedError",
    "HookHandler",
    "HookIdentity",
    "HookPoint",
    "HookRunState",
    "HookRunner",
    "HookSet",
    "ModelPostPayload",
    "ModelPrePayload",
    "SessionStartPayload",
    "ToolPostPayload",
    "ToolPrePayload",
    "bind_run_state",
    "finalize_run",
    "get_run_state",
    "record_model_response",
]
