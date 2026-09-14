# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.validation_result

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

命令校验结果数据类。

提供 ``ValidationResult`` 数据类，作为命令白名单校验的统一返回结构。
"""

from __future__ import annotations

from dataclasses import dataclass

# ========== 校验结果 ==========


@dataclass
class ValidationResult:
    """命令校验结果。"""

    is_allowed: bool
    """命令是否允许执行"""

    reason: str = ""
    """拒绝原因（当 is_allowed 为 False 时有效）"""

    rejected_command: str = ""
    """被拒绝的具体命令（用于审计日志）"""

    rejection_category: str = ""
    """拒绝分类（当 is_allowed 为 False 时有效）。

    用于命令级审批（Layer 2）区分「可审批的灰名单」与「不可审批的硬拒绝」：
    - ``"not_in_whitelist"`` —— 命令结构合法但不在白名单，可走命令级审批；
    - ``"forbidden_pattern"`` —— 命中禁止模式（命令替换 / 后台执行 / 重定向等），硬拒绝；
    - ``""``（默认）—— 空命令 / 空字节 / 注释 / 嵌套过深等其他拒绝，一律硬拒绝。
    """

    def __bool__(self) -> bool:
        return self.is_allowed
