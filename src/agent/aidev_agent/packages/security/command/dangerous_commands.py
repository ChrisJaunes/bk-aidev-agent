# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.dangerous_commands

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

显式危险命令黑名单（Layer 1 首拦硬阻断）。

与命令白名单（``packages.security.command.command_security``，默认拒绝）互补：白名单
拒绝时只能给出泛化的「不在允许列表」理由；本模块对**已知高危命令**给出结构化
分类（数据破坏 / 外泄 / 提权 / 系统状态 / 远程执行管道），供执行路径在进入
白名单校验前先做显式 deny——命中即硬拒绝，不可审批、不可放行。

危险模式（标签 / 分类 / 正则）收敛于 ``packages.security.dangerous_patterns`` 的
``COMMAND_PATTERNS`` 单一数据源，与 ``packages.security.reward_hacking`` 共享同一批危险
意图定义，避免重复维护。本模块面向「单条 shell 命令」的首拦硬阻断（``command``
字符串），与 ``reward_hacking`` 面向「跨工具重复危险意图」的展平文本检测分层不同、
数据形状不同。

公开接口：
- scan_dangerous_commands(): 扫描单条命令，返回危险命中列表
- DangerousCommandHit: 命中记录（label / category / matched）
- CATEGORY_*: 危险分类常量（转发自 ``packages.security.dangerous_patterns``）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from aidev_agent.packages.security.dangerous_patterns import (
    CATEGORY_DATA_DESTRUCTION,
    CATEGORY_EXFILTRATION,
    CATEGORY_PRIVILEGE,
    CATEGORY_REMOTE_EXEC,
    CATEGORY_SYSTEM_STATE,
    COMMAND_PATTERNS,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DangerousCommandHit:
    """单条危险命令命中记录。

    Attributes:
        label: 危险标签（如 ``rm_recursive_force``）。
        category: 危险分类（如 ``data_destruction``）。
        matched: 命中的原始片段（正则 ``match.group(0)``，用于审计展示）。
    """

    label: str
    category: str
    matched: str


def scan_dangerous_commands(command: Any) -> list[DangerousCommandHit]:
    """扫描单条 shell 命令，返回危险命中列表（空列表 = 未命中危险命令）。

    防御性处理：非字符串输入（None / bytes / 其他类型）直接返回空列表，
    与调用方约定一致——只有字符串才参与危险命令匹配。

    Args:
        command: 待扫描的命令字符串（防御性接受任意类型）。

    Returns:
        命中列表；命令为空白 / 非字符串时返回空列表。
    """
    if not isinstance(command, str) or not command.strip():
        return []
    hits: list[DangerousCommandHit] = []
    for pattern in COMMAND_PATTERNS:
        match = pattern.pattern.search(command)
        if match:
            hits.append(
                DangerousCommandHit(
                    label=pattern.label,
                    category=pattern.category,
                    matched=match.group(0),
                )
            )
    return hits


__all__ = [
    "DangerousCommandHit",
    "scan_dangerous_commands",
    "CATEGORY_DATA_DESTRUCTION",
    "CATEGORY_EXFILTRATION",
    "CATEGORY_PRIVILEGE",
    "CATEGORY_SYSTEM_STATE",
    "CATEGORY_REMOTE_EXEC",
]
