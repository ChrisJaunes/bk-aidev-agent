# -*- coding: utf-8 -*-
"""SDK 侧的 hook 接线：宿主往返总线 + 映射到 SDK ``HookSet`` 的处理器。

``HookConfig`` / 点位名 / 降级原因 / ``hook_degraded`` 事件名都是**纯值契约**，住在
:mod:`aidev_agent_cli.runtime.hook_contract`（父进程解析 ``hooks`` 配置、写快照，不需要 SDK）。
本模块只保留真正需要 SDK 的部分：

- ``HeadlessHookBus`` 把一次 ``emit`` 变成一次宿主往返，用 ``ag_ui`` 的 ``CustomEvent`` 产出降级事件；
- ``build_hook_set`` 把协议点位挂到 SDK hook 点位：``session_start`` → ``turn_start``（context
  以 ``SystemMessage`` 插在本轮用户输入之前，由模型节点并入 system prompt），``tool_pre`` /
  ``tool_post`` → 同名点位；``ask`` 在 ``tool_pre`` 处理器内调用 ``interrupt()`` 转人工。

这里把契约名再导出（``from ..hook_contract import ...``）只是为了让 sdk 内部引用集中——
**父进程侧一律从 ``runtime.hook_contract`` 取**。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from ag_ui.core import CustomEvent, EventType
from aidev_agent.core.hook import HookContext, HookPoint, HookSet
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.types import interrupt

from ..hook_contract import (
    ALL_HOOK_POINTS,
    CANCEL_POLL_INTERVAL,
    DEFAULT_TIMEOUT_MS,
    DEGRADE_HOST_ERROR,
    DEGRADE_INVALID_SHAPE,
    DEGRADE_TIMEOUT,
    DEGRADE_UNKNOWN_DECISION,
    HOOK_ASK_REASON,
    HOOK_DEGRADED_EVENT_NAME,
    MAX_TIMEOUT_MS,
    SESSION_START_CONTEXT_KEY,
    WAIT_CANCELLED,
    WAIT_REPLY,
    WAIT_TIMEOUT,
    HookConfig,
    point_value,
)

logger = logging.getLogger("aidev_agent_cli")


class HeadlessHookBus:
    """会话级 hook 总线：把一次 ``emit`` 变成一次宿主往返回路（REQ-18）。"""

    def __init__(
        self,
        *,
        config: HookConfig,
        session_code: str,
        channel: Any,
        writer: Any,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self._config = config
        self._session_code = session_code
        self._channel = channel
        self._writer = writer
        self._cancel_check = cancel_check
        self._lock = threading.Lock()
        self._request_id = ""
        self._turn_id = ""
        self._seq = 0
        self._ask_memo: dict[str, str] = {}

    # ---------------- 生命周期 ----------------

    def bind(self, *, request_id: str, turn_id: str) -> None:
        """绑定本轮请求：``#h<n>`` 由本轮请求 id 派生，``n`` 会话级自 1 递增。"""
        self._request_id = request_id
        self._turn_id = turn_id

    def reset_turn(self) -> None:
        """轮末清理 ``ask`` memo（重放不再重复上报的前提，方案 D1）。"""
        with self._lock:
            self._ask_memo.clear()

    # ---------------- 协议面 ----------------

    @property
    def session_code(self) -> str:
        return self._session_code

    @property
    def timeout_ms(self) -> int:
        return self._config.timeout_ms

    def enabled(self, point: Any) -> bool:
        return self._config.enabled(point)

    def points(self) -> frozenset[str]:
        return self._config.points()

    def emit(self, point: Any, payload: dict[str, Any]) -> dict[str, Any]:
        """上报一个点位并返回裁决；不可用时返回空 dict（该点按最宽松裁决继续）。"""
        name = point_value(point)
        if not self._config.enabled(name):
            return {}

        line_id = f"{self._request_id}#h{self._next_seq()}"
        params = {
            "session_code": self._session_code,
            "turn_id": self._turn_id,
            "point": name,
            "payload": payload,
        }
        started = time.monotonic()
        outcome, reply = self._channel.request(
            line_id=line_id,
            params=params,
            timeout_ms=self._config.timeout_ms,
            cancel_check=self._cancel_check,
        )
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if outcome == WAIT_CANCELLED:
            # 取消优先：不写 hook_degraded，由既有取消收尾产出取消终止事件（ACC-20）
            logger.info("[headless-hook] 等待期间被取消: point=%s, id=%s", name, line_id)
            return {}
        if outcome == WAIT_TIMEOUT:
            logger.warning(
                "[headless-hook] 等待超时: point=%s, id=%s, timeout_ms=%s", name, line_id, self._config.timeout_ms
            )
            self.notify_degraded(name, DEGRADE_TIMEOUT)
            return {}
        if not reply or not reply.get("ok"):
            logger.warning("[headless-hook] 宿主返回失败: point=%s, id=%s, elapsed_ms=%s", name, line_id, elapsed_ms)
            self.notify_degraded(name, DEGRADE_HOST_ERROR)
            return {}
        result = reply.get("result")
        if not isinstance(result, dict):
            logger.warning("[headless-hook] 裁决形状不合法: point=%s, id=%s", name, line_id)
            self.notify_degraded(name, DEGRADE_INVALID_SHAPE)
            return {}
        logger.info("[headless-hook] point=%s decision=%s elapsed_ms=%s", name, result.get("decision"), elapsed_ms)
        return result

    def notify_degraded(self, point: Any, reason: str) -> None:
        """写一条 ``hook_degraded`` CUSTOM 事件（不新增 ``EventType``）。"""
        event = CustomEvent(
            type=EventType.CUSTOM,
            name=HOOK_DEGRADED_EVENT_NAME,
            value={"point": point_value(point), "reason": reason},
        )
        self._writer.emit(event)

    # ---------------- ask 重放去重 ----------------

    def ask_decision(self, tool_use_id: str) -> str | None:
        with self._lock:
            return self._ask_memo.get(tool_use_id)

    def remember_ask(self, tool_use_id: str) -> None:
        with self._lock:
            self._ask_memo[tool_use_id] = "ask"

    # ---------------- 内部 ----------------

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq


# ============================== 辅助 ==============================


def apply_session_start(result: dict[str, Any] | None) -> str | None:
    """从 ``session_start`` 裁决里取 ``context``；形状不合法时返回 ``None``（不注入）。"""
    if not isinstance(result, dict):
        return None
    context = result.get(SESSION_START_CONTEXT_KEY)
    if context is None:
        return None
    if not isinstance(context, str):
        return None
    return context or None


# ============================== SDK HookSet 接线 ==============================

#: SDK 侧等待处理器的宽限：总线自己按 ``timeout_ms`` 收敛并写降级事件，SDK 上限只兜底。
HOOK_SET_GRACE_SECONDS = 1.0

#: ``tool_post`` 追加 context 时的分隔块标记（模型可见，标注来源为 hook）
HOOK_CONTEXT_SEPARATOR = "\n\n[hook context]\n"

_TOOL_PRE_DECISIONS = frozenset({"allow", "deny", "ask"})
_TOOL_POST_DECISIONS = frozenset({"accept", "block", "content", "context"})


def _normalize_decision(result: Any, allowed: frozenset[str], default: str) -> tuple[str, str | None]:
    """把宿主裁决归一为 ``(decision, degrade_reason)``；无法判定时取该点最宽松裁决。"""
    if result is None:
        return default, None
    if not isinstance(result, dict):
        return default, DEGRADE_INVALID_SHAPE
    decision = result.get("decision")
    if decision is None:
        return default, None
    normalized = decision.strip().lower() if isinstance(decision, str) else None
    if normalized not in allowed:
        return default, DEGRADE_UNKNOWN_DECISION
    return normalized, None


def interpret_tool_pre(result: Any) -> tuple[str, str | None]:
    """解释 ``tool_pre`` 裁决（``allow`` / ``deny`` / ``ask``）。"""
    return _normalize_decision(result, _TOOL_PRE_DECISIONS, "allow")


def interpret_tool_post(result: Any) -> tuple[str, str | None]:
    """解释 ``tool_post`` 裁决；``decision`` 缺省时由 ``content`` / ``context`` 推断。"""
    if isinstance(result, dict) and result.get("decision") is None:
        if result.get("content") is not None:
            return "content", None
        if result.get("context"):
            return "context", None
    return _normalize_decision(result, _TOOL_POST_DECISIONS, "accept")


def decode_ask_answer(answer: Any) -> str:
    """把 ``interrupt()`` 的恢复值解释为 ``allow`` / ``deny``；缺失或未知按 ``deny``。"""
    if isinstance(answer, dict):
        answer = answer.get("decision")
    if isinstance(answer, str) and answer.strip().lower() == "allow":
        return "allow"
    return "deny"


def _tool_response_text(message: ToolMessage) -> str:
    if isinstance(message.content, str):
        return message.content
    return json.dumps(message.content, ensure_ascii=False, default=str)


def _rewrite(message: ToolMessage, content: Any, *, status: str | None = None) -> ToolMessage:
    """改写同一条 ``ToolMessage``：``tool_call_id`` / ``name`` / ``additional_kwargs`` 不变。"""
    update: dict[str, Any] = {"content": content}
    if status is not None:
        update["status"] = status
    return message.model_copy(update=update)


def _denied(tool_call: dict[str, Any], reason: str) -> ToolMessage:
    return ToolMessage(
        content=reason, tool_call_id=tool_call.get("id") or "", name=tool_call.get("name") or "", status="error"
    )


def _with_context(messages: list[BaseMessage], context: str) -> list[BaseMessage]:
    """把 context 作为 ``SystemMessage`` 插到最后一条用户消息之前（无用户消息时追加到末尾）。"""
    index = next((i for i in range(len(messages) - 1, -1, -1) if isinstance(messages[i], HumanMessage)), len(messages))
    return [*messages[:index], SystemMessage(content=context), *messages[index:]]


def build_hook_set(bus: HeadlessHookBus, *, turn_id: str, input_text: str, first_turn: bool) -> HookSet | None:
    """按已启用的协议点位构造本轮 ``HookSet``；一个点位都没启用时返回 ``None``（不注入）。"""
    tool_inputs: dict[str, Any] = {}

    def session_start(context: HookContext) -> list[BaseMessage] | None:
        result = bus.emit(
            "session_start",
            {"session_code": bus.session_code, "turn_id": turn_id, "first_turn": first_turn, "input": input_text},
        )
        raw_context = result.get("context") if isinstance(result, dict) else None
        if raw_context is not None and not isinstance(raw_context, str):
            bus.notify_degraded("session_start", DEGRADE_INVALID_SHAPE)
        text = apply_session_start(result)
        return _with_context(list(context.payload or []), text) if text else None

    def tool_pre(context: HookContext) -> ToolMessage | None:
        tool_call = context.payload.tool_call
        tool_use_id = tool_call.get("id") or ""
        payload = {
            "tool_name": tool_call.get("name") or "",
            "tool_input": tool_call.get("args") or {},
            "tool_use_id": tool_use_id,
        }
        tool_inputs[tool_use_id] = payload["tool_input"]
        if not bus.enabled("tool_pre"):
            return None
        verdict: dict[str, Any] = {}
        if tool_use_id and bus.ask_decision(tool_use_id):
            # 重放轮（resume 后工具节点重跑）：同一 tool_use_id 只上报一次，直接回到 interrupt 取恢复值
            decision = "ask"
        else:
            result = bus.emit("tool_pre", payload)
            decision, degrade_reason = interpret_tool_pre(result)
            if degrade_reason:
                bus.notify_degraded("tool_pre", degrade_reason)
            verdict = result if isinstance(result, dict) else {}
        if decision == "deny":
            return _denied(tool_call, str(verdict.get("reason") or "hook 已拒绝该工具调用"))
        if decision == "ask":
            if tool_use_id:
                bus.remember_ask(tool_use_id)
            answer = interrupt(
                {
                    "reason": HOOK_ASK_REASON,
                    "message": verdict.get("reason") or f"工具 {payload['tool_name']} 需要人工确认",
                    "toolCallId": tool_use_id,
                }
            )
            if decode_ask_answer(answer) != "allow":
                return _denied(tool_call, "hook 人工裁决未放行，工具未执行")
        return None

    def tool_post(context: HookContext) -> ToolMessage | None:
        message: ToolMessage = context.payload
        result = bus.emit(
            "tool_post",
            {
                "tool_name": message.name or "",
                "tool_input": tool_inputs.get(message.tool_call_id, {}),
                "tool_use_id": message.tool_call_id,
                "tool_response": _tool_response_text(message),
            },
        )
        decision, degrade_reason = interpret_tool_post(result)
        if degrade_reason:
            bus.notify_degraded("tool_post", degrade_reason)
        verdict = result if isinstance(result, dict) else {}
        if decision == "block":
            return _rewrite(message, str(verdict.get("reason") or "hook 已阻断该工具结果"), status="error")
        context_text = verdict.get("context")
        if decision == "content":
            content = verdict.get("content", message.content)
            return _rewrite(message, f"{content}{HOOK_CONTEXT_SEPARATOR}{context_text}" if context_text else content)
        if decision == "context" and context_text:
            return _rewrite(message, f"{message.content}{HOOK_CONTEXT_SEPARATOR}{context_text}")
        return None

    handlers: dict[HookPoint, list] = {}
    if bus.enabled("session_start"):
        handlers[HookPoint.TURN_START] = [session_start]
    if bus.enabled("tool_pre") or bus.enabled("tool_post"):
        handlers[HookPoint.TOOL_PRE] = [tool_pre]
    if bus.enabled("tool_post"):
        handlers[HookPoint.TOOL_POST] = [tool_post]
    if not handlers:
        return None
    return HookSet(handlers, timeout_seconds=bus.timeout_ms / 1000 + HOOK_SET_GRACE_SECONDS)


__all__ = [
    "ALL_HOOK_POINTS",
    "CANCEL_POLL_INTERVAL",
    "DEFAULT_TIMEOUT_MS",
    "DEGRADE_HOST_ERROR",
    "DEGRADE_INVALID_SHAPE",
    "DEGRADE_TIMEOUT",
    "HOOK_CONTEXT_SEPARATOR",
    "HOOK_DEGRADED_EVENT_NAME",
    "HeadlessHookBus",
    "HookConfig",
    "MAX_TIMEOUT_MS",
    "SESSION_START_CONTEXT_KEY",
    "WAIT_CANCELLED",
    "WAIT_REPLY",
    "WAIT_TIMEOUT",
    "apply_session_start",
    "build_hook_set",
    "decode_ask_answer",
    "interpret_tool_post",
    "interpret_tool_pre",
    "point_value",
]
