# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.dangerous_patterns

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

危险模式单一数据源。

收敛 ``dangerous_commands``（单条 shell 命令首拦硬阻断）与 ``reward_hacking``
（跨工具重复危险意图检测）各自维护的 `(label, regex)` 危险模式表，消除两处
重复维护的同一批危险意图正则。

每条模式为 :class:`DangerousPattern`（label / category / pattern）三元组：

- ``label`` / ``pattern`` 两处共享：决定「是否命中」与「命中标签」；
- ``category`` 仅 ``dangerous_commands`` 使用（审计分类标签），
  ``reward_hacking`` 只消费 ``label + pattern``，忽略 ``category``。

共享项取两者正则的**并集**（更宽的一方），因此重构后不减少任何原有命中，
只会对个别标签（如 ``mkfs_format`` 纳入 ``format``、``shutdown_reboot`` 纳入
``poweroff``/``halt`` 等）略微扩大匹配范围——方向为更严格，不改变对外接口。

公开接口：

- ``COMMAND_PATTERNS``：命令黑名单扫描用（共享 + 命令独有，共 12 条）。
- ``REWARD_PATTERNS``：reward hacking 检测用（共享 + reward 独有，共 11 条）。
- ``DangerousPattern``：具名三元组。
- ``CATEGORY_*``：危险分类常量（由 ``dangerous_commands`` 转发导出）。
"""

from __future__ import annotations

import re
from typing import NamedTuple

# 危险命令分类（供审计标签 / 策略聚合使用）
CATEGORY_DATA_DESTRUCTION = "data_destruction"  # 数据破坏
CATEGORY_EXFILTRATION = "exfiltration"  # 数据外泄
CATEGORY_PRIVILEGE = "privilege"  # 提权 / 账户管理
CATEGORY_SYSTEM_STATE = "system_state"  # 系统状态破坏
CATEGORY_REMOTE_EXEC = "remote_exec"  # 远程执行管道（下载即执行）


class DangerousPattern(NamedTuple):
    """单条危险模式三元组：标签 / 分类 / 正则。"""

    label: str
    category: str
    pattern: re.Pattern


# 共享危险模式（dangerous_commands 与 reward_hacking 均使用；正则取两者并集）。
# 正则只匹配「危险意图」的最小特征，不做完整命令语法解析（语法解析归白名单）。
# 保持模式偏窄，避免误伤正常运维命令。
_SHARED_PATTERNS: tuple[DangerousPattern, ...] = (
    DangerousPattern(
        "rm_recursive_force",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\brm\b[^\n]{0,40}(?:-rf|-fr)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "mkfs_format",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\b(?:mkfs(?:\.\w+)?|format)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "shutdown_reboot",
        CATEGORY_SYSTEM_STATE,
        re.compile(r"\b(?:shutdown|reboot|poweroff|halt)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "chmod_777",
        CATEGORY_PRIVILEGE,
        re.compile(r"\bchmod\s+777\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "user_management",
        CATEGORY_PRIVILEGE,
        re.compile(r"\b(?:useradd|adduser|usermod|passwd|visudo)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "firewall_change",
        CATEGORY_SYSTEM_STATE,
        re.compile(r"\b(?:iptables|ufw)\s+-", re.IGNORECASE),
    ),
    DangerousPattern(
        "curl_pipe_shell",
        CATEGORY_REMOTE_EXEC,
        re.compile(r"\bcurl\b[^\n]{0,80}\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "wget_pipe_shell",
        CATEGORY_REMOTE_EXEC,
        re.compile(r"\bwget\b[^\n]{0,80}\|\s*(?:sh|bash|zsh)\b", re.IGNORECASE),
    ),
)

# 命令黑名单独有模式（仅 ``scan_dangerous_commands`` 扫描单条 shell 命令时使用）
_COMMAND_ONLY_PATTERNS: tuple[DangerousPattern, ...] = (
    DangerousPattern(
        "dd_disk_write",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\bdd\b[^\n]{0,60}\bof=/dev/(?:sd|vd|hd|nvme|mmcblk|xvd)", re.IGNORECASE),
    ),
    DangerousPattern(
        "shred_file",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\bshred\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "exfil_curl_token",
        CATEGORY_EXFILTRATION,
        re.compile(r"\bcurl\b[^\n]{0,120}\b(?:token|secret|password|authorization|api[-_]?key)\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "chown_root",
        CATEGORY_PRIVILEGE,
        re.compile(r"\bchown\b[^\n]{0,40}\broot\b", re.IGNORECASE),
    ),
)

# reward hacking 独有模式（仅 ``detect_dangerous_intent`` 扫描展平文本时使用）
_REWARD_ONLY_PATTERNS: tuple[DangerousPattern, ...] = (
    DangerousPattern(
        "drop_table",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "truncate_table",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\btruncate\s+table\b", re.IGNORECASE),
    ),
    DangerousPattern(
        "delete_from",
        CATEGORY_DATA_DESTRUCTION,
        re.compile(r"\bdelete\s+from\b", re.IGNORECASE),
    ),
)

# 命令黑名单扫描模式表（共享 + 命令独有）
COMMAND_PATTERNS: tuple[DangerousPattern, ...] = _SHARED_PATTERNS + _COMMAND_ONLY_PATTERNS

# reward hacking 检测模式表（共享 + reward 独有）
REWARD_PATTERNS: tuple[DangerousPattern, ...] = _SHARED_PATTERNS + _REWARD_ONLY_PATTERNS

__all__ = [
    "DangerousPattern",
    "COMMAND_PATTERNS",
    "REWARD_PATTERNS",
    "CATEGORY_DATA_DESTRUCTION",
    "CATEGORY_EXFILTRATION",
    "CATEGORY_PRIVILEGE",
    "CATEGORY_SYSTEM_STATE",
    "CATEGORY_REMOTE_EXEC",
]
