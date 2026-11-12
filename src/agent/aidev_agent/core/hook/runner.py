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

hook 执行器：按注入顺序执行、合并裁决、短路、fail-open、单处理器超时与结构化日志。

执行语义（REQ-6）：
- 同一点位多个处理器按注入顺序执行；首个 ``deny`` / ``ask`` 短路，后续处理器不再执行。
- 其余裁决中，后写覆盖先写（最后一次 ``replace`` / ``append`` 生效）。
- 处理器抛异常、超时（``TIMEOUT_SECONDS``）、返回未知裁决或缺少必需载荷时按 ``pass``
  处理并记 warning，同点位其余处理器与业务链路继续。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from contextvars import copy_context
from typing import Any

from .contexts import HookContext, HookIdentity
from .decisions import PASS, HookDecision, HookDecisionKind
from .points import ALLOWED_DECISION_KINDS, HookPoint
from .registry import HookHandler, HookSet

__all__ = ["HookRunner"]

logger = logging.getLogger(__name__)

# 处理器抛错 / 超时的降级原因
_DEGRADED_EXCEPTION = "exception"
_DEGRADED_TIMEOUT = "timeout"
_DEGRADED_INVALID_DECISION = "invalid_decision"
_DEGRADED_INVALID_PAYLOAD = "invalid_payload"


def _handler_id(handler: HookHandler) -> str:
    """处理器标识：优先限定名，用于结构化日志与隔离断言。"""
    return getattr(handler, "__qualname__", None) or getattr(handler, "__name__", None) or type(handler).__name__


def _degrade(point: HookPoint, handler: HookHandler, reason: str) -> HookDecision:
    logger.warning("[Hook] degraded point=%s handler=%s reason=%s", point.value, _handler_id(handler), reason)
    return PASS


class HookRunner:
    """按点位执行处理器集合的运行器。

    ``HookSet`` 在构建期固化，运行器本身不持有可变状态，可按实例独立创建。
    """

    TIMEOUT_SECONDS = 5.0

    def __init__(self, hooks: HookSet | None = None) -> None:
        self._hooks = hooks or HookSet()

    @property
    def hooks(self) -> HookSet:
        return self._hooks

    def has(self, point: HookPoint) -> bool:
        """该点位是否注册了处理器；为 False 时调用方应整体跳过。"""
        return bool(self._hooks.handlers(point))

    def run_sync(self, point: HookPoint, identity: HookIdentity, payload: Any) -> HookDecision:
        """同步执行该点位的处理器。"""
        handlers = self._hooks.handlers(point)
        if not handlers:
            return PASS
        final = PASS
        for handler in handlers:
            decision = self._execute_sync(point, identity, payload, handler)
            if decision.kind in (HookDecisionKind.DENY, HookDecisionKind.ASK):
                return decision
            if decision.kind in (HookDecisionKind.REPLACE, HookDecisionKind.APPEND):
                final = decision
        return final

    async def run_async(self, point: HookPoint, identity: HookIdentity, payload: Any) -> HookDecision:
        """异步执行该点位的处理器；裁决语义与 :meth:`run_sync` 一致。"""
        handlers = self._hooks.handlers(point)
        if not handlers:
            return PASS
        final = PASS
        for handler in handlers:
            decision = await self._execute_async(point, identity, payload, handler)
            if decision.kind in (HookDecisionKind.DENY, HookDecisionKind.ASK):
                return decision
            if decision.kind in (HookDecisionKind.REPLACE, HookDecisionKind.APPEND):
                final = decision
        return final

    # ------------------------------------------------------------------
    # 单处理器执行
    # ------------------------------------------------------------------
    def _execute_sync(
        self, point: HookPoint, identity: HookIdentity, payload: Any, handler: HookHandler
    ) -> HookDecision:
        context = HookContext(point=point, identity=identity, payload=payload)
        started = time.monotonic()
        try:
            raw = self._call_with_timeout(handler, context)
        except TimeoutError:
            decision = _degrade(point, handler, _DEGRADED_TIMEOUT)
        except Exception:
            logger.warning(
                "[Hook] degraded point=%s handler=%s reason=%s",
                point.value,
                _handler_id(handler),
                _DEGRADED_EXCEPTION,
                exc_info=True,
            )
            decision = PASS
        else:
            decision = self._normalize(point, handler, raw)
        self._log(point, identity, handler, decision, started, payload)
        return decision

    async def _execute_async(
        self, point: HookPoint, identity: HookIdentity, payload: Any, handler: HookHandler
    ) -> HookDecision:
        context = HookContext(point=point, identity=identity, payload=payload)
        started = time.monotonic()
        try:
            raw = await self._call_with_timeout_async(handler, context)
        except (asyncio.TimeoutError, TimeoutError):
            decision = _degrade(point, handler, _DEGRADED_TIMEOUT)
        except Exception:
            logger.warning(
                "[Hook] degraded point=%s handler=%s reason=%s",
                point.value,
                _handler_id(handler),
                _DEGRADED_EXCEPTION,
                exc_info=True,
            )
            decision = PASS
        else:
            decision = self._normalize(point, handler, raw)
        self._log(point, identity, handler, decision, started, payload)
        return decision

    @classmethod
    def _call_with_timeout(cls, handler: HookHandler, context: HookContext) -> Any:
        """在独立线程执行处理器，超过 ``TIMEOUT_SECONDS`` 按超时处理。

        线程无法被强杀：超时后仅放弃等待并按 pass 返回，处理器若永久阻塞仍会占一个
        线程（fail-open 已保证业务不受影响，线程泄漏风险见方案「风险」一节）。
        线程内以 ``copy_context().run`` 执行，处理器可见调用方的 contextvars（追踪等）。
        """
        box: dict[str, Any] = {}

        def invoke() -> None:
            try:
                box["value"] = cls._resolve_awaitable(handler(context))
            except Exception as error:  # noqa: BLE001 - 原样回传给等待线程；BaseException 不穿透 fail-open
                box["error"] = error

        run = copy_context().run
        thread = threading.Thread(target=run, args=(invoke,), name="aidev-hook-handler", daemon=True)
        thread.start()
        thread.join(cls.TIMEOUT_SECONDS)
        if thread.is_alive():
            raise TimeoutError(f"hook handler exceeded {cls.TIMEOUT_SECONDS}s")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    @classmethod
    async def _call_with_timeout_async(cls, handler: HookHandler, context: HookContext) -> Any:
        if inspect.iscoroutinefunction(handler):
            result = await asyncio.wait_for(handler(context), timeout=cls.TIMEOUT_SECONDS)
            return await result if inspect.isawaitable(result) else result
        result = await asyncio.wait_for(
            asyncio.to_thread(handler, context),
            timeout=cls.TIMEOUT_SECONDS,
        )
        return await result if inspect.isawaitable(result) else result

    @staticmethod
    def _resolve_awaitable(value: Any) -> Any:
        """同步线程里遇到协程处理器时，用独立事件循环执行。"""
        if not inspect.isawaitable(value):
            return value

        async def consume() -> Any:
            return await value

        return asyncio.run(consume())

    # ------------------------------------------------------------------
    # 裁决校验与日志
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize(point: HookPoint, handler: HookHandler, raw: Any) -> HookDecision:
        if raw is None:
            return PASS
        if not isinstance(raw, HookDecision):
            return _degrade(point, handler, _DEGRADED_INVALID_DECISION)

        try:
            kind = HookDecisionKind(raw.kind)
        except ValueError:
            return _degrade(point, handler, _DEGRADED_INVALID_DECISION)

        if kind is HookDecisionKind.PASS:
            return PASS
        if kind.value not in ALLOWED_DECISION_KINDS.get(point, frozenset()):
            return _degrade(point, handler, _DEGRADED_INVALID_DECISION)
        if kind in (HookDecisionKind.DENY, HookDecisionKind.ASK):
            if not isinstance(raw.reason, str) or not raw.reason.strip():
                return _degrade(point, handler, _DEGRADED_INVALID_PAYLOAD)
            return raw
        if raw.payload is None:
            return _degrade(point, handler, _DEGRADED_INVALID_PAYLOAD)
        if point is HookPoint.SESSION_START and not isinstance(raw.payload, str):
            return _degrade(point, handler, _DEGRADED_INVALID_PAYLOAD)
        return raw

    @staticmethod
    def _log(
        point: HookPoint,
        identity: HookIdentity,
        handler: HookHandler,
        decision: HookDecision,
        started: float,
        payload: Any,
    ) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        tool_name = getattr(payload, "tool_name", "")
        logger.info(
            "[Hook] point=%s handler=%s decision=%s duration_ms=%s agent=%s session=%s run=%s turn=%s tool=%s",
            point.value,
            _handler_id(handler),
            decision.kind.value,
            duration_ms,
            identity.agent_code,
            identity.session_code,
            identity.run_id,
            identity.turn,
            tool_name,
        )
