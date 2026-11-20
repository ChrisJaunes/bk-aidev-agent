# -*- coding: utf-8 -*-
"""测试替身：把「脚本化的事件流」装成会话 agent（统一执行路径下的唯一注入缝）。

脚本沿用旧 runner 替身的形状：``async def impl(params, is_cancelled)``，逐条产出事件
（SSE 帧 ``data: {...}\n\n``、裸 JSON 字符串或 dict 都可以）。替身 agent 在会话的工作线程
里跑完脚本，把每条事件交给会话的 event_handler——之后的一切（终止事件补齐、sink 线程桥、
取消、结果）都是生产实现。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any


def strip_sse_shell(chunk: str) -> str:
    """去掉 SSE 帧的 ``data: `` 前缀与结尾空行；不是 SSE 的原样（去空格）返回。"""
    text = chunk.strip()
    payload_lines: list[str] = []
    saw_data = False
    for line in text.splitlines():
        if line.startswith("data:"):
            saw_data = True
            payload_lines.append(line[len("data:") :].lstrip())
        elif line.startswith(":"):
            continue
        elif saw_data:
            payload_lines.append(line)
    if not saw_data:
        return text
    return "\n".join(payload_lines)


def _payload(chunk: Any) -> Any:
    if isinstance(chunk, dict):
        return chunk
    try:
        return json.loads(strip_sse_shell(chunk) if isinstance(chunk, str) else chunk)
    except (json.JSONDecodeError, TypeError):
        return None


class ScriptedAgent:
    """``execute`` 跑脚本并把事件交给 event_handler；``stop`` 只记一下。"""

    def __init__(self, impl: Any, session: Any, handler: Any, carried: Any) -> None:
        self._impl = impl
        self._session = session
        self.event_handler = handler
        self._carried = carried
        self.stopped = False
        self.executed: list[Any] = []

    def execute(self, kwargs: Any) -> Any:
        self.executed.append(kwargs)
        writer = self._session.writer
        params = {
            "reqId": writer.request_id,
            "runId": writer.request_id,
            "input": kwargs.input,
            "sessionCode": kwargs.session_code,
            "executeKwargs": self._carried,
        }

        async def _go() -> None:
            async for chunk in self._impl(params, self._session._cancel_requested_now):
                payload = _payload(chunk)
                if payload is not None:
                    self.event_handler(payload)

        asyncio.run(_go())
        return iter(())

    def stop(self) -> None:
        self.stopped = True


def script_session(session: Any, impl: Any) -> Any:
    """把一个会话的 agent 换成脚本替身（实例级覆盖，不影响其它会话）。"""
    carried_box: dict[str, Any] = {"value": None}
    original_turn = session.turn

    def _turn(**kwargs: Any) -> Any:
        carried_box["value"] = kwargs.get("execute_kwargs")
        return original_turn(**kwargs)

    def _build_agent(**params: Any) -> Any:
        return ScriptedAgent(impl, session, params.get("event_handler") or session.writer, carried_box["value"])

    session.turn = _turn
    session.build_agent = _build_agent
    session._event_handler = lambda: session.writer
    session.ensure_platform_session = lambda *args, **kwargs: None
    return session


__all__ = ["ScriptedAgent", "script_session", "strip_sse_shell"]


def thread_safe_sink(sink: Any, loop: asyncio.AbstractEventLoop) -> Any:
    """Unit-test bridge for worker-local synchronous SDK semantics, not production execution."""

    from aidev_agent_cli.runtime.models import event_payload
    from aidev_agent_cli.runtime.runner import _call_sink

    def _deliver(event: Any) -> None:
        if getattr(sink, "peer_gone", False):
            return
        payload = event_payload(event)
        future = asyncio.run_coroutine_threadsafe(_call_sink(sink, payload), loop)
        try:
            future.result(timeout=30)
        except Exception as exc:  # noqa: BLE001 - 对端断开不应打断运行
            raise RuntimeError(f"unit-test event delivery failed: {exc}") from exc

    return _deliver
