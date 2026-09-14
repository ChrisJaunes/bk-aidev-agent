# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command

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

命令执行安全四件套（聚合子包）。

收拢命令执行侧的四层安全能力：

- ``command_security`` —— 命令白名单（Layer 0，默认拒绝）+ 文件路径校验；
- ``dangerous_commands`` —— 危险命令黑名单（Layer 1，命中即硬拒绝）；
- ``command_approval`` —— 命令级审批（Layer 2，灰名单 HITL）；
- ``command_risk_assessor`` —— 审批 smart 模式的风险评估器（辅助 LLM 三级预分流）。

本子包同时承载命令执行前后的**编排**入口 ``enforce_command_security``
（三层防护：黑名单 → 白名单 → 命令级审批），供 ``core.tools.runtime_tools``
的 execute 工具调用。

本子包只做公开接口聚合转发，具体实现仍分别在各自模块中；依赖方向沿用
``packages.security`` 约定：仅标准库 / pydantic / langchain_core / ``pydantic_models`` / 本包内模块。
"""

from __future__ import annotations

from aidev_agent.packages.security.command.command_approval import (
    command_fingerprint,
    get_command_approval_mode,
    require_command_approval,
)
from aidev_agent.packages.security.command.command_risk_assessor import (
    CommandRiskAssessor,
    RiskAssessment,
)
from aidev_agent.packages.security.command.command_security import (
    ALLOWED_COMMANDS,
    DEFAULT_ALLOWED_SCRIPT_DIRS,
    EFFECTIVE_ALLOWED_COMMANDS,
    ENSURE_NON_EMPTY_HINT,
    PARAMETER_RESTRICTIONS,
    AllowedFlagsOnly,
    ForbiddenFlags,
    ValidationResult,
    enforce_command_security,
    ensure_non_empty,
    is_command_allowed,
    validate_command,
    validate_path,
)
from aidev_agent.packages.security.command.dangerous_commands import (
    CATEGORY_DATA_DESTRUCTION,
    CATEGORY_EXFILTRATION,
    CATEGORY_PRIVILEGE,
    CATEGORY_REMOTE_EXEC,
    CATEGORY_SYSTEM_STATE,
    DangerousCommandHit,
    scan_dangerous_commands,
)

__all__ = [
    # 命令白名单 / 校验
    "ValidationResult",
    "AllowedFlagsOnly",
    "ForbiddenFlags",
    "ALLOWED_COMMANDS",
    "PARAMETER_RESTRICTIONS",
    "EFFECTIVE_ALLOWED_COMMANDS",
    "DEFAULT_ALLOWED_SCRIPT_DIRS",
    "validate_command",
    "is_command_allowed",
    "validate_path",
    # 命令安全编排（三层防护）
    "enforce_command_security",
    "ensure_non_empty",
    "ENSURE_NON_EMPTY_HINT",
    # 危险命令黑名单
    "DangerousCommandHit",
    "scan_dangerous_commands",
    "CATEGORY_DATA_DESTRUCTION",
    "CATEGORY_EXFILTRATION",
    "CATEGORY_PRIVILEGE",
    "CATEGORY_SYSTEM_STATE",
    "CATEGORY_REMOTE_EXEC",
    # 命令级审批
    "require_command_approval",
    "get_command_approval_mode",
    "command_fingerprint",
    # 命令风险评估
    "CommandRiskAssessor",
    "RiskAssessment",
]
