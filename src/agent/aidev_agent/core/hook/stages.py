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

点位阶段函数：供模型节点、pv 节点调用，把「协议」与「节点实现」解耦。

- :func:`apply_session_and_model_pre`：装配完成后、首次请求发出前执行 ``session_start``
  与 ``model_pre``，并把 ``append`` / ``replace`` 应用到本次实际发送的消息列表。
- :func:`apply_model_post` / :func:`apply_model_post_async`：每轮最终答复返回后触发
  ``model_post``，``replace`` 改写本轮输出。
- :func:`run_tool_pre_stage`：pv 阶段按模型给出的顺序逐个触发 ``tool_pre``，裁决按
  ``tool_call_id`` 存入运行态，由工具 wrapper 应用。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig

from .contexts import ModelPostPayload, ModelPrePayload, SessionStartPayload, ToolPrePayload
from .decisions import HookDecisionKind
from .errors import HookDeniedError
from .points import HookPoint
from .run_state import get_run_state
from .runner import HookRunner

__all__ = [
    "apply_session_and_model_pre",
    "apply_model_post",
    "apply_model_post_async",
    "run_tool_pre_stage",
]

logger = logging.getLogger(__name__)


def _ensure_message_ids(messages: list[BaseMessage]) -> None:
    """给缺失 id 的历史消息补 id，供覆盖视图的锚点判定使用（仅注入 hooks 时生效）。"""
    for message in messages:
        if not getattr(message, "id", None):
            message.id = uuid.uuid4().hex


def _tail_after_anchor(messages: list[BaseMessage], anchor_ids: frozenset[str]) -> list[BaseMessage]:
    """取锚点之后新增的尾部消息；锚点为空时原样返回。"""
    if not anchor_ids:
        return list(messages)
    last_anchor = -1
    for index, message in enumerate(messages):
        if getattr(message, "id", None) in anchor_ids:
            last_anchor = index
    return list(messages[last_anchor + 1 :])


def _insert_system_fragment(messages: list[BaseMessage], fragment: str) -> list[BaseMessage]:
    """把追加片段作为独立 SystemMessage 插到 system 段末尾（首个非 system 消息之前）。"""
    for message in messages:
        if isinstance(message, SystemMessage) and message.content == fragment:
            return messages
    insert_at = 0
    for index, message in enumerate(messages):
        if not isinstance(message, SystemMessage):
            insert_at = index
            break
    else:
        insert_at = len(messages)
    return [*messages[:insert_at], SystemMessage(content=fragment), *messages[insert_at:]]


def _last_user_input(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, HumanMessage):
            content = message.content
            return content if isinstance(content, str) else str(content)
    return ""


def _model_name(llm: Any) -> str:
    for attr in ("model_name", "model"):
        value = getattr(llm, attr, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _tool_names(tools: Any) -> tuple[str, ...]:
    return tuple(getattr(tool, "name", "") for tool in (tools or []))


def apply_session_and_model_pre(ctx: Any, hooks: HookRunner | None) -> None:
    """执行 ``session_start`` 与 ``model_pre``，并把裁决结果写入 ``ctx.messages``。

    ``session_start`` 在本次运行首轮首次请求前恰好一次；``model_pre`` 在每轮首次请求前
    恰好一次（运行态去重，recovery 重试与工具回灌后的请求不再触发）。

    Raises:
        HookDeniedError: 任一处理器返回 ``deny``，调用方不得发出本次请求。
    """
    if hooks is None:
        return
    state = get_run_state(ctx.config)
    if state is None:
        return

    messages = list(ctx.messages)
    tool_names = _tool_names(getattr(ctx, "tools", None))
    identity = state.identity()

    if not state.session_started:
        payload = SessionStartPayload(
            message_count=len(messages),
            last_user_input=_last_user_input(messages),
            tool_names=tool_names,
        )
        decision = hooks.run_sync(HookPoint.SESSION_START, identity, payload)
        if decision.kind is HookDecisionKind.DENY:
            raise HookDeniedError(HookPoint.SESSION_START, decision.reason)
        if decision.kind is HookDecisionKind.APPEND:
            state.session_append = decision.payload
        state.session_started = True

    if state.session_append:
        messages = _insert_system_fragment(messages, state.session_append)

    if not state.model_pre_done:
        payload = ModelPrePayload(
            messages=list(messages),
            model_name=_model_name(getattr(ctx, "llm", None)),
            tool_names=tool_names,
            turn=state.turn,
        )
        decision = hooks.run_sync(HookPoint.MODEL_PRE, identity, payload)
        if decision.kind is HookDecisionKind.DENY:
            raise HookDeniedError(HookPoint.MODEL_PRE, decision.reason)
        if decision.kind is HookDecisionKind.REPLACE:
            _ensure_message_ids(messages)
            state.model_pre_override = list(decision.payload)
            state.model_pre_anchor_ids = frozenset(message.id for message in messages if getattr(message, "id", None))
        state.model_pre_done = True

    if state.model_pre_override is not None:
        messages = [*state.model_pre_override, *_tail_after_anchor(messages, state.model_pre_anchor_ids)]
        if state.session_append:
            messages = _insert_system_fragment(messages, state.session_append)

    ctx.messages = messages


def _model_post_payload(state: Any, response: Any) -> ModelPostPayload:
    return ModelPostPayload(
        response=response,
        model_request_count=state.model_request_count,
        turn=state.turn,
    )


def apply_model_post(ctx: Any, hooks: HookRunner | None) -> None:
    """每轮最终答复返回后触发一次 ``model_post``（含工具调用的中间响应不触发）。"""
    state = _prepare_model_post(ctx, hooks)
    if state is None:
        return
    payload = _model_post_payload(state, ctx.response)
    decision = hooks.run_sync(HookPoint.MODEL_POST, state.identity(), payload)
    _apply_model_post_decision(state, ctx, decision)


async def apply_model_post_async(ctx: Any, hooks: HookRunner | None) -> None:
    """``apply_model_post`` 的异步版本，裁决语义一致。"""
    state = _prepare_model_post(ctx, hooks)
    if state is None:
        return
    payload = _model_post_payload(state, ctx.response)
    decision = await hooks.run_async(HookPoint.MODEL_POST, state.identity(), payload)
    _apply_model_post_decision(state, ctx, decision)


def _prepare_model_post(ctx: Any, hooks: HookRunner | None) -> Any:
    if hooks is None:
        return None
    state = get_run_state(ctx.config)
    if state is None:
        return None
    response = getattr(ctx, "response", None)
    if response is None or getattr(response, "tool_calls", None):
        return None
    return state


def _apply_model_post_decision(state: Any, ctx: Any, decision: Any) -> None:
    if decision.kind is HookDecisionKind.DENY:
        raise HookDeniedError(HookPoint.MODEL_POST, decision.reason)
    if decision.kind is HookDecisionKind.REPLACE and isinstance(decision.payload, str):
        ctx.response = ctx.response.model_copy(update={"content": decision.payload})
    state.model_post_done = True


def run_tool_pre_stage(state_values: dict, config: RunnableConfig, tools: Any = None) -> None:
    """pv 阶段串行触发 ``tool_pre``，裁决按 ``tool_call_id`` 存入运行态。

    只对「模型已给出、尚未产出 ToolMessage」的调用触发，顺序即模型给出的顺序；
    参数改写不在此处落地，循环守卫仍按模型原始参数判定。
    """
    run_state = get_run_state(config)
    if run_state is None or run_state.runner is None:
        return
    # 延迟导入：避免 core.hook 与 core.nodes 包初始化期互相引用
    from aidev_agent.core.nodes.tool.tool_call_proxy_wrapper import resolve_real_tool_call

    messages = state_values.get("messages") or []
    if not messages:
        return
    last = messages[-1]
    if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
        return

    done_ids = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    tools_by_name = {getattr(tool, "name", ""): tool for tool in (tools or [])}
    runner = run_state.runner
    identity = run_state.identity()

    for tool_call in last.tool_calls:
        tool_call_id = tool_call.get("id") or ""
        if not tool_call_id or tool_call_id in done_ids or tool_call_id in run_state.tool_pre_decisions:
            continue
        tool_name, arguments = resolve_real_tool_call(tool_call, tools_by_name)
        payload = ToolPrePayload(
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            arguments=arguments,
            turn=run_state.turn,
        )
        run_state.tool_pre_decisions[tool_call_id] = runner.run_sync(HookPoint.TOOL_PRE, identity, payload)
