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

安全 hook 事件与判定类型。

本模块是 ``packages/security`` 外置安全 hook 框架的类型基石，只定义纯数据
契约（枚举 / dataclass），不依赖 ``core`` / ``services`` / ``api``。安平现成
服务（内容安全、DLP、威胁情报、注入检测等）以 :class:`SecurityHook` 形式接入，
在 :class:`SecurityStage` 定义的检查点上被统一触发。

设计原则：

- **外置**：hook 实现与注册都落在 ``packages/security``，``core`` 只通过
  :func:`~aidev_agent.packages.security.hooks.run_hooks` 触发事件，不感知具体
  接入的服务。
- **fail-closed**：判定动作按 ``block`` > ``review`` > ``allow`` 聚合，任一
  hook 要求拦截即整体拦截。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class SecurityStage(str, Enum):
    """安全事件阶段（hook 的触发检查点）。

    - ``user_input``：用户原始输入（进模型前）
    - ``tool_result``：工具返回结果（进模型上下文前）
    - ``model_output``：模型产出文本（交付给用户前）
    - ``command``：命令执行（白名单 / 黑名单判定前）
    - ``file_send``：文件 / 产物发送（交付给用户前）
    """

    USER_INPUT = "user_input"
    TOOL_RESULT = "tool_result"
    MODEL_OUTPUT = "model_output"
    COMMAND = "command"
    FILE_SEND = "file_send"


@dataclass(frozen=True)
class SecurityEvent:
    """一次安全检测事件。

    Args:
        stage: 触发阶段。
        content: 待检测内容（文本 / 命令 / 文件内容）。
        metadata: 附加上下文（文件名、工具名、命令来源等），hook 可据此细粒度判定。
    """

    stage: SecurityStage
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SecurityVerdict:
    """安全判定结果。

    Args:
        action: 判定动作 —— ``allow``（放行）/ ``review``（转人工审批）/ ``block``（硬拦截）。
        hook: 产生该判定的 hook 名（聚合时保留最严格者的来源）。
        reason: 命中原因（用于日志 / 上报 / 兜底回复）。
        findings: 命中明细（关键词、类别、证据等，可扩展）。
    """

    action: Literal["allow", "block", "review"]
    hook: str = ""
    reason: str = ""
    findings: list[Any] = field(default_factory=list)


# 动作优先级：block > review > allow（用于多 hook 聚合）
_ACTION_PRIORITY: dict[str, int] = {"allow": 0, "review": 1, "block": 2}


def stricter(a: SecurityVerdict, b: SecurityVerdict) -> SecurityVerdict:
    """返回更严格的一方（用于聚合多个 hook 判定）。"""
    return a if _ACTION_PRIORITY[a.action] >= _ACTION_PRIORITY[b.action] else b


__all__ = [
    "SecurityStage",
    "SecurityEvent",
    "SecurityVerdict",
    "stricter",
]
