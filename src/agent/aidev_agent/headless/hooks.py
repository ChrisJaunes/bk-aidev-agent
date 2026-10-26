# -*- coding: utf-8 -*-
"""headless 侧的 hook 通道：配置解析、答复路由、同步往返总线（REQ-18 / REQ-19）。

职责边界（REQ-13「执行归属」）：本仓只上报「哪个点发生了 + 载荷」、接收「裁决」。
解析 ``hooks.json``、跑命令、matcher 合并与审计全部属宿主，本模块不代为实现。

协议两类新行（REQ-18）：
- agent → host：``{"id": "<本轮请求 id>#h<n>", "type": "request", "method": "hook", "params": {...}}``
- host → agent：``{"id": "...", "ok": true, "result": {...}}`` / ``{"id": "...", "ok": false, "error": {...}}``

fail-open 规则（REQ-18）：超时 / 宿主 ``ok=false`` / ``result`` 形状不合法 → 按该点
最宽松裁决继续，并各写恰好一条 ``hook_degraded``（复用既有 ``CUSTOM`` 事件，不新增
``EventType``）。等待期间收到 ``cancel`` 时立刻返回且不写 ``hook_degraded``。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from ag_ui.core import CustomEvent, EventType

from aidev_agent.core.hooks import (
    ALL_HOOK_POINTS,
    DEGRADE_HOST_ERROR,
    DEGRADE_INVALID_SHAPE,
    DEGRADE_TIMEOUT,
    HOOK_DEGRADED_EVENT_NAME,
    SESSION_START_CONTEXT_KEY,
    point_value,
)

from .errors import HOOK_VALUE_INVALID, HeadlessError

logger = logging.getLogger("aidev-agent.headless")

#: 单点等待裁决的缺省上限（毫秒）
DEFAULT_TIMEOUT_MS = 10000
#: 超时上限（1 小时）——超过按上限钳制
MAX_TIMEOUT_MS = 3_600_000
#: 等待期间巡检取消标志的粒度（秒）
CANCEL_POLL_INTERVAL = 0.05

#: 等待结果
WAIT_REPLY = "reply"
WAIT_TIMEOUT = "timeout"
WAIT_CANCELLED = "cancelled"


class HookConfig:
    """``create.params.hooks`` 解析结果（REQ-13：超时上限 + 启用点位）。"""

    def __init__(self, *, timeout_ms: int = DEFAULT_TIMEOUT_MS, points: frozenset[str] | None = None) -> None:
        self.timeout_ms = timeout_ms
        self._points = frozenset(points) if points is not None else frozenset(ALL_HOOK_POINTS)

    @classmethod
    def parse(cls, raw: Any) -> "HookConfig":
        """解析 ``hooks`` 配置；形状不合法一律 ``hook.value_invalid``。"""
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
            points = frozenset(ALL_HOOK_POINTS)
        else:
            if not isinstance(raw_points, (list, tuple, set, frozenset)):
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    "hooks.points 类型不符（期望字符串列表）",
                    {"fields": ["hooks.points"]},
                )
            unknown = [item for item in raw_points if item not in ALL_HOOK_POINTS]
            if unknown:
                raise HeadlessError(
                    HOOK_VALUE_INVALID,
                    "hooks.points 含未知点位：只支持 session_start / tool_pre / tool_post",
                    {"fields": ["hooks.points"], "unknown": sorted(str(item) for item in unknown)},
                )
            points = frozenset(raw_points)
        return cls(timeout_ms=timeout_ms, points=points)

    def enabled(self, point: Any) -> bool:
        return point_value(point) in self._points

    def points(self) -> frozenset[str]:
        return self._points


class _PendingSlot:
    """一条 hook 请求的等待槽位。"""

    __slots__ = ("event", "payload")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.payload: dict[str, Any] | None = None


class HookPendingRouter:
    """答复行路由：``id`` → 等待方（``threading.Event`` 唤醒）。

    答复行不产生响应行、不进入请求解析、不被报成未知方法；未知 / 畸形答复行只记
    调试日志，绝不使进程退出（REQ-18 / ACC-19）。
    """

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
        """投递一条答复行；``id`` 未命中时返回 ``False``（不报错、不退出）。"""
        if not isinstance(payload, dict):
            return False
        line_id = payload.get("id")
        if not isinstance(line_id, str) or not line_id:
            logger.debug("[headless-hook] 答复行缺少 id，忽略")
            return False
        with self._lock:
            slot = self._slots.get(line_id)
        if slot is None:
            logger.debug("[headless-hook] 答复行未命中等待方：id=%s", line_id)
            return False
        slot.payload = payload
        slot.event.set()
        return True

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._slots)


class HookChannel:
    """写出 hook 请求行并同步等待答复（共用事件写锁，不与其他行交错）。"""

    def __init__(self, *, write: Callable[[str], None], router: HookPendingRouter) -> None:
        self._write = write
        self._router = router

    def request(
        self,
        *,
        line_id: str,
        params: dict[str, Any],
        timeout_ms: int,
        cancel_check: Callable[[], bool] | None = None,
    ) -> tuple[str, dict[str, Any] | None]:
        """写请求行并等待答复，返回 ``(WAIT_*, 答复行)``。"""
        from .protocol import encode_hook_request

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


class HeadlessHookBus:
    """会话级 hook 总线：把一次 ``emit`` 变成一次宿主往返回路（REQ-18）。"""

    def __init__(
        self,
        *,
        config: HookConfig,
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
            logger.warning("[headless-hook] 等待超时: point=%s, id=%s, timeout_ms=%s", name, line_id, self._config.timeout_ms)
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


__all__ = [
    "CANCEL_POLL_INTERVAL",
    "DEFAULT_TIMEOUT_MS",
    "HookChannel",
    "HookConfig",
    "HookPendingRouter",
    "HeadlessHookBus",
    "MAX_TIMEOUT_MS",
    "WAIT_CANCELLED",
    "WAIT_REPLY",
    "WAIT_TIMEOUT",
    "apply_session_start",
]
