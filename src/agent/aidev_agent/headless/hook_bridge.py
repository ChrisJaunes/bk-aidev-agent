# -*- coding: utf-8 -*-
"""headless 宿主 hook 通道：把进程内 ``HookSet`` 变成一次同步 NDJSON 往返。

develop 的 hook 机制是**进程内**的（``core/hook``：6 点位 + ``HookSet`` + ``HookRunner``），
处理器入参 ``HookContext.payload`` 是 LangChain / LangGraph 原生对象。外部宿主（dsh）在
另一个进程里，因此 headless 侧补这一层传输：

- 每个点位注册一个处理器，收到点位时把载荷归一成 JSON 写一条
  ``{"type":"request","method":"hook","params":{...}}``，再阻塞等待宿主答复；
- 宿主答复 ``{"id":...,"ok":true,"result":{...}}`` 后，按点位把裁决翻译回原生对象
  （``ToolMessage`` / ``SystemMessage`` / ``HookRetry``）；
- 超时、宿主失败、形状不合法一律 fail-open（返回 ``None`` 表示不干预），并写一条
  ``hook_degraded`` CUSTOM 事件，不新增 ``EventType``。

注意 ``HookRunner`` 对单个处理器有 5s 上限（``HookRunner.TIMEOUT_SECONDS``），
所以本层的等待上限必须严格小于它（默认 3s，最大 4s）。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable

from ag_ui.core import CustomEvent, EventType
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage

from aidev_agent.core.hook import HookPoint, HookRetry
from aidev_agent.core.hook.registry import HookSet

from .errors import HOOK_VALUE_INVALID, HeadlessError

logger = logging.getLogger("aidev-agent.headless")

#: 单点等待上限（毫秒）。必须小于 ``HookRunner.TIMEOUT_SECONDS``（5s）。
DEFAULT_TIMEOUT_MS = 3000
MAX_TIMEOUT_MS = 4000
#: 等待期间巡检取消标志的粒度（秒）
CANCEL_POLL_INTERVAL = 0.05

#: 支持的 (protocol point) 集合
ALL_POINTS: frozenset[str] = frozenset(point.value for point in HookPoint)

#: 等待结果
WAIT_REPLY = "reply"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"

#: 降级原因（写进 ``hook_degraded`` 事件的 ``value.reason``）
DEGRADE_TIMEOUT = "timeout"
DEGRADE_HOST_ERROR = "host_error"
DEGRADE_INVALID_SHAPE = "invalid_shape"
HOOK_DEGRADED_EVENT_NAME = "hook_degraded"


class HookBridgeConfig:
    """``create.params.hooks`` 解析结果：等待上限 + 启用点位。"""

    def __init__(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS, points: frozenset[str] | None = None) -> None:
        self.timeout_ms = timeout_ms
        self._points = frozenset(points) if points is not None else frozenset(ALL_POINTS)

    @classmethod
    def parse(cls, raw: Any) -> "HookBridgeConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise HeadlessError(
                HOOK_VALUE_INVALID,
                f"hooks 配置类型不符（期望 object，实际 {type(raw).__name__}）",
                {"fields": ["hooks"]},
            )
        timeout_ms = raw.get("timeout_ms", DEFAULT_TIMEOUT_MS)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
            raise HeadlessError(
                HOOK_VALUE_INVALID,
                "hooks.timeout_ms 必须是正整数（毫秒）",
                {"fields": ["hooks.timeout_ms"]},
            )
        timeout_ms = min(timeout_ms, MAX_TIMEOUT_MS)

        raw_points = raw.get("points")
        if raw_points is None:
            points = frozenset(ALL_POINTS)
        else:
            if not isinstance(raw_points, (list, tuple, set, frozenset)):
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    "hooks.points 类型不符（期望字符串列表）",
                    {"fields": ["hooks.points"]},
                )
            unknown = [item for item in raw_points if item not in ALL_POINTS]
            if unknown:
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    "hooks.points 含未知点位：只支持 " + " / ".join(sorted(ALL_POINTS)),
                    {"fields": ["hooks.points"], "unknown": sorted(str(item) for item in unknown)},
                )
            points = frozenset(raw_points)
        return cls(timeout_ms=timeout_ms, points=points)

    def enabled(self, point: Any) -> bool:
        raw = point.value if isinstance(point, HookPoint) else str(point)
        return raw in self._points

    def points(self) -> frozenset[str]:
        return self._points


class _PendingSlot:
    """一条 hook 请求的等待槽位。"""

    __slots__ = ("event", "payload")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.payload: dict[str, Any] | None = None


class HookPendingRouter:
    """答复行路由：``id`` → 等待方；未命中的答复行只记调试日志。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[str, _PendingSlot] = {}

    def register(self, line_id: str) -> _PendingSlot:
        slot = _PendingSlot()
        with self._lock:
            self._slots[line_id] = slot
        return slot

    def discard(self, line_id: str) -> None:
        with self._lock:
            self._slots.pop(line_id, None)

    def deliver(self, payload: dict[str, Any]) -> bool:
        if not isinstance(payload, dict):
            return False
        line_id = payload.get("id")
        if not isinstance(line_id, str) or not line_id:
            return False
        with self._lock:
            slot = self._slots.get(line_id)
        if slot is None:
            return False
        slot.payload = payload
        slot.event.set()
        return True


class HookChannel:
    """写出 hook 请求行并同步等待答复（与事件行共用同一写锁）。"""

    def __init__(self, *, write: Callable[[str], None], router: HookPendingRouter) -> None:
        self._write = write
        self._router = router

    def request(
        self,
        *,
        line_id: str,
        params: dict[str, Any],
        timeout_ms: int,
        wait: bool = True,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        from .protocol import encode_hook_request

        if not wait:
            self._write(encode_hook_request(line_id, params))
            return WAIT_CANCELLED, None

        slot = self._router.register(line_id)
        try:
            self._write(encode_hook_request(line_id, params))
            deadline = time.monotonic() + timeout_ms / 1000.0
            while True:
                if slot.event.wait(CANCEL_POLL_INTERVAL):
                    return WAIT_REPLY, slot.payload
                if cancel_check is not None and cancel_check():
                    return WAIT_CANCELLED, None
                if time.monotonic() >= deadline:
                    return WAIT_TIMEOUT, None
        finally:
            self._router.discard(line_id)


# ============================== 载荷归一 ==============================


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(str(block))
        return "".join(parts)
    return str(content or "")


def serialize_messages(messages: Any) -> list[dict[str, Any]]:
    """把 ``list[BaseMessage]`` 归一成宿主可读的 JSON。"""
    result: list[dict[str, Any]] = []
    for message in messages or []:
        item: dict[str, Any] = {
            "role": getattr(message, "type", None) or getattr(message, "role", "") or "",
            "content": _text_of(getattr(message, "content", "")),
        }
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            item["tool_calls"] = [
                {
                    "id": str(call.get("id") or ""),
                    "name": str(call.get("name") or ""),
                    "arguments": call.get("args") or {},
                }
                for call in tool_calls
                if isinstance(call, dict)
            ]
        result.append(item)
    return result


def serialize_tool_request(request: Any) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", None) or {}
    return {
        "tool_name": tool_call.get("name") or "",
        "tool_input": tool_call.get("args") or {},
        "tool_use_id": tool_call.get("id") or "",
    }


def serialize_tool_message(message: ToolMessage) -> dict[str, Any]:
    return {
        "tool_name": getattr(message, "name", "") or "",
        "tool_use_id": getattr(message, "tool_call_id", "") or "",
        "tool_response": _text_of(getattr(message, "content", "")),
        "status": getattr(message, "status", None) or "success",
    }


# ============================== 桥 ==============================


class HookBridge:
    """把 6 个点位接成宿主往返；未启用点位不注册处理器（零行为变化）。"""

    def __init__(
        self,
        *,
        config: HookBridgeConfig,
        session_code: str,
        channel: HookChannel,
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

    # ---------------- 生命周期 ----------------

    def bind(self, *, request_id: str, turn_id: str) -> None:
        self._request_id = request_id
        self._turn_id = turn_id

    def hook_set(self) -> HookSet:
        """按启用点位构造 ``HookSet``（只注册启用的点）。"""
        handlers: dict[HookPoint, list[Callable[[Any], Any]]] = {}
        if self._config.enabled(HookPoint.TURN_START):
            handlers[HookPoint.TURN_START] = [self._on_turn_start]
        if self._config.enabled(HookPoint.MODEL_PRE):
            handlers[HookPoint.MODEL_PRE] = [self._on_model_pre]
        if self._config.enabled(HookPoint.MODEL_POST):
            handlers[HookPoint.MODEL_POST] = [self._on_model_post]
        if self._config.enabled(HookPoint.TOOL_PRE):
            handlers[HookPoint.TOOL_PRE] = [self._on_tool_pre]
        if self._config.enabled(HookPoint.TOOL_POST):
            handlers[HookPoint.TOOL_POST] = [self._on_tool_post]
        if self._config.enabled(HookPoint.TURN_END):
            handlers[HookPoint.TURN_END] = [self._on_turn_end]
        return HookSet(handlers)

    # ---------------- 6 个处理器 ----------------

    def _on_turn_start(self, context: Any) -> list[BaseMessage] | None:
        messages = list(context.payload or [])
        result = self._emit(
            HookPoint.TURN_START,
            {"messages": serialize_messages(messages), "input": _last_user_text(messages)},
        )
        return _with_context(messages, result)

    def _on_model_pre(self, context: Any) -> list[BaseMessage] | None:
        messages = list(getattr(context.payload, "messages", None) or [])
        result = self._emit(HookPoint.MODEL_PRE, {"messages": serialize_messages(messages)})
        return _with_context(messages, result)

    def _on_model_post(self, context: Any) -> Any:
        response = getattr(context.payload, "response", None)
        result = self._emit(
            HookPoint.MODEL_POST,
            {
                "content": _text_of(getattr(response, "content", "")),
                "tool_calls": (getattr(response, "tool_calls", None) or []),
            },
        )
        if isinstance(result, dict) and result.get("retry"):
            raise HookRetry(str(result.get("reason") or "host requested retry"))
        return None

    def _on_tool_pre(self, context: Any) -> Any:
        request = context.payload
        payload = serialize_tool_request(request)
        result = self._emit(HookPoint.TOOL_PRE, payload)
        decision = result.get("decision") if isinstance(result, dict) else None
        if decision in ("deny", "ask"):
            # 新机制没有 ask 通道：宿主返回 ask 时按 deny 处理，并在 reason 里说明
            reason = str(result.get("reason") or "hook 拒绝了该工具调用")
            if decision == "ask":
                reason = f"{reason}（当前 hook 机制不支持 ask，按拒绝处理）"
            return ToolMessage(
                content=reason,
                tool_call_id=payload["tool_use_id"] or "",
                name=payload["tool_name"] or "",
                status="error",
            )
        return None

    def _on_tool_post(self, context: Any) -> ToolMessage | None:
        message = context.payload
        payload = serialize_tool_message(message)
        result = self._emit(HookPoint.TOOL_POST, payload)
        decision = result.get("decision") if isinstance(result, dict) else None
        if decision in ("block", "deny"):
            return ToolMessage(
                content=str(result.get("reason") or "hook 阻断了该工具结果"),
                tool_call_id=payload["tool_use_id"],
                name=payload["tool_name"],
                status="error",
            )
        if decision == "content":
            content = result.get("content", payload["tool_response"])
            context_text = result.get("context")
            if context_text:
                content = f"{content}\n\n[hook context]\n{context_text}"
            return ToolMessage(
                content=content,
                tool_call_id=payload["tool_use_id"],
                name=payload["tool_name"],
                status=payload["status"],
            )
        if decision == "context" and result.get("context"):
            return ToolMessage(
                content=f"{payload['tool_response']}\n\n[hook context]\n{result['context']}",
                tool_call_id=payload["tool_use_id"],
                name=payload["tool_name"],
                status=payload["status"],
            )
        return None

    def _on_turn_end(self, context: Any) -> None:
        # 收尾通知：不等答复（HookRunner 已在退出路径上，不能被宿主拖住）
        self._emit(HookPoint.TURN_END, {"turn_id": self._turn_id}, wait=False)
        return None

    # ---------------- 往返 ----------------

    def _emit(self, point: HookPoint, payload: dict[str, Any], *, wait: bool = True) -> dict[str, Any]:
        if not self._config.enabled(point):
            return {}
        with self._lock:
            self._seq += 1
            seq = self._seq
        line_id = f"{self._request_id}#h{seq}"
        params = {
            "session_code": self._session_code,
            "turn_id": self._turn_id,
            "point": point.value,
            "payload": payload,
        }
        started = time.monotonic()
        outcome, reply = self._channel.request(
            line_id=line_id,
            params=params,
            timeout_ms=self._config.timeout_ms,
            wait=wait,
            cancel_check=self._cancel_check,
        )
        if not wait:
            return {}
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if outcome == WAIT_CANCELLED:
            logger.info("[headless-hook] 等待期间被取消: point=%s id=%s", point.value, line_id)
            return {}
        if outcome == WAIT_TIMEOUT:
            logger.warning("[headless-hook] 等待超时: point=%s id=%s timeout_ms=%s", point.value, line_id, self._config.timeout_ms)
            self.notify_degraded(point.value, DEGRADE_TIMEOUT)
            return {}
        if not reply or not reply.get("ok"):
            logger.warning("[headless-hook] 宿主返回失败: point=%s id=%s elapsed_ms=%s", point.value, line_id, elapsed_ms)
            self.notify_degraded(point.value, DEGRADE_HOST_ERROR)
            return {}
        result = reply.get("result")
        if not isinstance(result, dict):
            self.notify_degraded(point.value, DEGRADE_INVALID_SHAPE)
            return {}
        logger.info("[headless-hook] point=%s decision=%s elapsed_ms=%s", point.value, result.get("decision"), elapsed_ms)
        return result

    def notify_degraded(self, point: str, reason: str) -> None:
        """写一条 ``hook_degraded`` CUSTOM 事件（不新增 ``EventType``）。"""
        try:
            self._writer.emit(
                CustomEvent(type=EventType.CUSTOM, name=HOOK_DEGRADED_EVENT_NAME, value={"point": point, "reason": reason})
            )
        except Exception:  # pylint: disable=broad-except - 降级观测失败不得影响业务
            logger.warning("[headless-hook] 写 hook_degraded 失败", exc_info=True)


def _last_user_text(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if getattr(message, "type", "") == "human":
            return _text_of(getattr(message, "content", ""))
    return ""


def _with_context(messages: list[BaseMessage], result: Any) -> list[BaseMessage] | None:
    """宿主返回 ``context`` 时，在最前面插一条 SystemMessage（不改原消息对象）。"""
    if not isinstance(result, dict):
        return None
    context = result.get("context")
    if not isinstance(context, str) or not context:
        return None
    return [SystemMessage(content=context), *messages]


__all__ = [
    "ALL_POINTS",
    "DEFAULT_TIMEOUT_MS",
    "DEGRADE_HOST_ERROR",
    "DEGRADE_INVALID_SHAPE",
    "DEGRADE_TIMEOUT",
    "HOOK_DEGRADED_EVENT_NAME",
    "HookBridge",
    "HookBridgeConfig",
    "HookChannel",
    "HookPendingRouter",
    "MAX_TIMEOUT_MS",
    "serialize_messages",
    "serialize_tool_message",
    "serialize_tool_request",
]
