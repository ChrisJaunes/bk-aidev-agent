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

工具点位 wrapper：

- ``tool_pre`` 应用 wrapper：位于代理解包之后、不可信净化 / 脱敏 / ITSM 审批之前，
  按 pv 阶段存入运行态的裁决执行 ``deny`` / ``ask`` / ``replace``。循环守卫在链首，
  先于本次改写判定，因此参数改写的语义不影响守卫输入。
- ``tool_post`` 应用 wrapper：位于 wrapper 链最内层（``deduplicate`` 与 ``_content_error``
  之内），处理器看到未脱敏原始结果；其输出继续流经 ``_content_error`` 重新识别错误、
  去重、结果截断、脱敏与不可信净化。
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone

from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import AsyncToolCallWrapper, ToolCallRequest, ToolCallWrapper
from langgraph.types import Command, interrupt

from aidev_agent.core.hook.contexts import ToolPostPayload
from aidev_agent.core.hook.decisions import HookDecision, HookDecisionKind
from aidev_agent.core.hook.points import HookPoint
from aidev_agent.core.hook.run_state import get_run_state, restore_run_state
from aidev_agent.core.hook.runner import HookRunner
from aidev_agent.packages.interrupt_manager.ask_user_question import (
    AskUserQuestionTarget,
    parse_resume_answers,
)
from aidev_agent.utils.tool_result import is_error_content

__all__ = [
    "build_tool_pre_sync_wrapper",
    "build_tool_pre_async_wrapper",
    "build_tool_post_sync_wrapper",
    "build_tool_post_async_wrapper",
]

HOOK_ASK_ALLOW_LABEL = "允许执行"
HOOK_ASK_DENY_LABEL = "拒绝执行"
HOOK_ASK_HEADER = "工具执行确认"


def _runtime_config(request: ToolCallRequest):
    runtime = getattr(request, "runtime", None)
    return getattr(runtime, "config", None)


def _iter_result_messages(result: ToolMessage | Command) -> list[ToolMessage]:
    if isinstance(result, ToolMessage):
        return [result]
    if isinstance(result, Command) and isinstance(result.update, dict):
        return [each for each in result.update.get("messages", []) or [] if isinstance(each, ToolMessage)]
    return []


def _deny_message(request: ToolCallRequest, reason: str) -> ToolMessage:
    """构造拒绝 / 未批准的失败结果（工具未执行）。"""
    tool_call = request.tool_call
    return ToolMessage(
        content=reason,
        tool_call_id=tool_call.get("id") or "",
        name=request.tool.name if request.tool is not None else tool_call.get("name") or "",
        status="error",
    )


def _ask_approved(request: ToolCallRequest, decision: HookDecision) -> bool:
    """走既有 ``ask_user_question`` 中断通道请求人工裁决；非「允许执行」一律视为未批准。"""
    tool_call = request.tool_call
    tool_name = request.tool.name if request.tool is not None else tool_call.get("name") or ""
    target = AskUserQuestionTarget(
        questions=[
            {
                "header": HOOK_ASK_HEADER,
                "multiSelect": False,
                "question": f"是否允许执行工具 {tool_name}？{decision.reason}",
                "options": [
                    {"label": HOOK_ASK_ALLOW_LABEL, "description": "批准后继续执行该工具调用"},
                    {"label": HOOK_ASK_DENY_LABEL, "description": "拒绝执行，工具不会运行"},
                ],
            }
        ],
        message="hook tool_pre 请求人工裁决",
        toolCallId=tool_call.get("id") or "",
        expiresAt=(datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
    )
    answers = parse_resume_answers(interrupt(target.model_dump()))
    if not answers:
        return False
    text = answers if isinstance(answers, str) else json.dumps(answers, ensure_ascii=False, default=str)
    return HOOK_ASK_ALLOW_LABEL in text


def _tool_pre_decision(request: ToolCallRequest) -> HookDecision | None:
    config = _runtime_config(request)
    state = get_run_state(config)
    if state is None:
        return None
    # 续流后 pv 阶段不会重跑：从图状态快照续接同一批次已下发的裁决
    restore_run_state(getattr(request, "state", None), config)
    tool_call_id = request.tool_call.get("id") or ""
    return state.tool_pre_decisions.get(tool_call_id)


def _apply_pre_decision(request: ToolCallRequest, decision: HookDecision) -> ToolCallRequest | ToolMessage:
    """按 tool_pre 裁决决定继续执行（可能改写参数）或直接返回失败结果。"""
    if decision.kind is HookDecisionKind.DENY:
        return _deny_message(request, decision.reason)
    if decision.kind is HookDecisionKind.ASK and not _ask_approved(request, decision):
        return _deny_message(request, f"hook ask not approved at tool_pre: {decision.reason}")
    if decision.kind is HookDecisionKind.REPLACE and isinstance(decision.payload, dict):
        tool_call = {**request.tool_call, "args": dict(decision.payload)}
        return request.override(tool_call=tool_call)
    return request


def _apply_post_decision(
    message: ToolMessage,
    decision: HookDecision,
) -> None:
    """按 tool_post 裁决就地改写结果消息（``tool_call_id`` / ``name`` / 元数据不变）。"""
    if decision.kind is HookDecisionKind.DENY:
        message.content = decision.reason
        message.status = "error"
        return
    if decision.kind is HookDecisionKind.REPLACE:
        message.content = decision.payload
        return
    if decision.kind is HookDecisionKind.APPEND:
        if isinstance(message.content, str):
            message.content = f"{message.content}\n\n{decision.payload}"
        elif isinstance(message.content, list):
            message.content = [*message.content, {"type": "text", "text": str(decision.payload)}]
        else:
            message.content = f"{message.content}\n\n{decision.payload}"


def _run_tool_post(request: ToolCallRequest, result: ToolMessage | Command, duration_ms: int, hooks: HookRunner):
    state = get_run_state(_runtime_config(request))
    if state is None:
        return
    identity = state.identity()
    tool_name = request.tool.name if request.tool is not None else request.tool_call.get("name") or ""
    for message in _iter_result_messages(result):
        payload = ToolPostPayload(
            tool_name=tool_name,
            tool_call_id=message.tool_call_id or request.tool_call.get("id") or "",
            content=message.content,
            is_error=message.status == "error" or is_error_content(message.content),
            duration_ms=duration_ms,
            turn=state.turn,
        )
        decision = hooks.run_sync(HookPoint.TOOL_POST, identity, payload)
        if decision.kind is not HookDecisionKind.PASS:
            _apply_post_decision(message, decision)


async def _arun_tool_post(request: ToolCallRequest, result: ToolMessage | Command, duration_ms: int, hooks: HookRunner):
    state = get_run_state(_runtime_config(request))
    if state is None:
        return
    identity = state.identity()
    tool_name = request.tool.name if request.tool is not None else request.tool_call.get("name") or ""
    for message in _iter_result_messages(result):
        payload = ToolPostPayload(
            tool_name=tool_name,
            tool_call_id=message.tool_call_id or request.tool_call.get("id") or "",
            content=message.content,
            is_error=message.status == "error" or is_error_content(message.content),
            duration_ms=duration_ms,
            turn=state.turn,
        )
        decision = await hooks.run_async(HookPoint.TOOL_POST, identity, payload)
        if decision.kind is not HookDecisionKind.PASS:
            _apply_post_decision(message, decision)


def build_tool_pre_sync_wrapper(hooks: HookRunner) -> ToolCallWrapper:
    """``tool_pre`` 应用 wrapper（同步）。"""

    def wrapper(
        request: ToolCallRequest, execute: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        decision = _tool_pre_decision(request)
        if decision is None:
            return execute(request)
        applied = _apply_pre_decision(request, decision)
        if isinstance(applied, ToolMessage):
            return applied
        return execute(applied)

    return wrapper


def build_tool_pre_async_wrapper(hooks: HookRunner) -> AsyncToolCallWrapper:
    """``tool_pre`` 应用 wrapper（异步）。"""

    async def wrapper(
        request: ToolCallRequest, execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        decision = _tool_pre_decision(request)
        if decision is None:
            return await execute(request)
        applied = _apply_pre_decision(request, decision)
        if isinstance(applied, ToolMessage):
            return applied
        return await execute(applied)

    return wrapper


def build_tool_post_sync_wrapper(hooks: HookRunner) -> ToolCallWrapper:
    """``tool_post`` wrapper（同步，位于链最内层）。"""

    def wrapper(
        request: ToolCallRequest, execute: Callable[[ToolCallRequest], ToolMessage | Command]
    ) -> ToolMessage | Command:
        started = int(time.monotonic() * 1000)
        result = execute(request)
        _run_tool_post(request, result, int(time.monotonic() * 1000) - started, hooks)
        return result

    return wrapper


def build_tool_post_async_wrapper(hooks: HookRunner) -> AsyncToolCallWrapper:
    """``tool_post`` wrapper（异步，位于链最内层）。"""

    async def wrapper(
        request: ToolCallRequest, execute: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]]
    ) -> ToolMessage | Command:
        started = int(time.monotonic() * 1000)
        result = await execute(request)
        await _arun_tool_post(request, result, int(time.monotonic() * 1000) - started, hooks)
        return result

    return wrapper
