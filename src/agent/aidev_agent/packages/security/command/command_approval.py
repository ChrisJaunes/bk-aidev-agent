# -*- coding: utf-8 -*-
"""aidev_agent.packages.security.command.command_approval

TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - AIDev (BlueKing - AIDev) available.
Copyright (C) 2025 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the " License ");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.

命令级审批（Layer 2 HITL）。

在命令既未命中危险命令黑名单（``packages.security.command.dangerous_commands``）、也未命中命令
白名单（``packages.security.command.command_security``）时——即「灰名单」命令——由执行
路径调用本模块，经 ``interrupt()`` 挂起图执行并走 ITSM 审批。

与工具级审批（``core.nodes.tool.approval_wrapper``）的区别：工具级审批在工具
调用前对「整个工具」做一次性审批（execute 工具若配置了 approval 元数据，每次
调用都需审批）；本模块在工具**内部**对「单条命令」做分级审批——白名单直行、
黑名单硬拒绝、灰名单才审批，避免审批疲劳被利用。

复用现有审批设施：构造 :class:`~aidev_agent.packages.interrupt_manager.approval.ApprovalTarget`
（``target_type="command"``，toolName/toolCode 均为 ``execute``，args 携带命令
与目标运行时），经 ``interrupt()`` 挂起；流结束侧由既有 ``ApprovalHandler``
建 ITSM 工单，resume 侧由既有编排校验终态后回放。

安全语义（fail-closed）：未启用 / 未配置审批人 / 非字符串命令 / interrupt
异常，一律返回 False（拒绝执行），绝不因审批能力缺失而放行。

公开接口：
- require_command_approval(): 对单条灰名单命令发起审批，返回是否放行
- command_fingerprint(): 归一化命令指纹（审计追踪用）
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from langgraph.types import interrupt

from aidev_agent.packages.interrupt_manager.approval import (
    TOOL_APPROVAL_REASON,
    ApprovalTarget,
)
from aidev_agent.pydantic_models import SecuritySettings

logger = logging.getLogger(__name__)


def get_command_approval_mode(security_settings: SecuritySettings) -> str:
    """读取命令审批模式 ``command_approval_mode``，默认 ``manual``。

    - ``manual``：灰名单命令全量走 ITSM 审批（保守兜底）。
    - ``smart``：辅助 LLM 预分流（low 放行 / high 拒绝 / uncertain 走 ITSM）。

    未知取值一律回退 ``manual``（fail-closed，不因配置错误引入自动放行）。

    Args:
        security_settings: 安全配置，由调用方显式传入（来自 ``AgentConfig.security_settings``）。

    Returns:
        ``"manual"`` 或 ``"smart"``。
    """
    mode = security_settings.command_approval_mode
    return mode if mode in ("manual", "smart") else "manual"


def _command_approvers(security_settings: SecuritySettings) -> list[str]:
    """读取命令级审批人（安全配置 ``command_approval_approvers``，逗号分隔，去空去重）。"""
    raw = security_settings.command_approval_approvers
    return list(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))


def command_fingerprint(command: str) -> str:
    """归一化命令指纹（去空白 / 统一小写），供审计追踪与 ``toolCallId`` 使用。"""
    normalized = re.sub(r"\s+", " ", command.strip().lower())
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:16]


def _decision_is_approved(decision: Any) -> bool:
    """解析 ``interrupt()`` 返回的审批 decision，判定是否放行。

    镜像 ``core.nodes.tool.approval_wrapper._is_approved`` 的解析语义：
    decision 可能是 list[dict]（取首元素）、dict（含 ``payload.approved``
    或 ``status``），非 dict / 无终态一律判定为拒绝（fail-closed）。
    """
    if isinstance(decision, list) and decision:
        decision = decision[0]
    if not isinstance(decision, dict):
        return False
    payload = decision.get("payload") if isinstance(decision.get("payload"), dict) else decision
    if "approved" in payload:
        return payload["approved"] is True
    status = payload.get("status") or decision.get("status")
    return status in (True, "approved", "resolved", "approve")


def require_command_approval(
    command: Any,
    *,
    target_runtime: str = "",
    security_settings: SecuritySettings,
) -> bool:
    """对单条灰名单命令发起 ITSM 审批（HITL），返回是否放行。

    Args:
        command: 待审批的命令字符串（防御性接受任意类型；非字符串拒绝）。
        target_runtime: 目标运行时标识（写入审批单 ``toolArgs``，便于审计）。
        security_settings: 安全配置，由调用方显式传入（来自 ``AgentConfig.security_settings``）。

    Returns:
        True 表示人工审批通过、可继续执行；False 表示拒绝 / 未配置 / 异常。
    """
    if not isinstance(command, str) or not command.strip():
        return False
    approvers = _command_approvers(security_settings)
    if not approvers:
        logger.warning("[CommandApproval] 未配置审批人，fail-closed 拒绝: command=%s", command[:200])
        return False

    approval_cfg = {
        "approvers": approvers,
        "tool_type": "command",
        "tool_name": "execute",
        "tool_code": "execute",
    }
    target = ApprovalTarget(
        target_type="command",
        target_id=command_fingerprint(command),
        target_name="execute",
        target_code="execute",
        args={"command": command, "target_runtime": target_runtime},
        approval=approval_cfg,
    )
    value = {**target.model_dump(by_alias=True), "reason": TOOL_APPROVAL_REASON}
    try:
        decision = interrupt(value)
    except Exception:
        # 无 LangGraph 上下文 / 中断异常：fail-closed（绝不因审批能力缺失放行）
        logger.exception("[CommandApproval] interrupt 异常，fail-closed 拒绝: command=%s", command[:200])
        return False
    return _decision_is_approved(decision)


__all__ = [
    "get_command_approval_mode",
    "command_fingerprint",
    "require_command_approval",
]
