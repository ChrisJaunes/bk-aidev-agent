# -*- coding: utf-8 -*-
"""事件写出：把 ``ag_ui.core`` 事件逐行写成 NDJSON（REQ-7）。

``SessionEventWriter`` 同时承担三件事，且三者互不依赖：
1. **协议写出**——默认写 stdout；注入 ``event_sink`` 时改为交给调用方（测试内存收集器）；
2. **账本采集**——本轮 assistant 文本与终止状态始终在内部记录，与 ``event_sink`` 解耦；
3. **终止事件补齐**——本轮必须以终止类事件收尾，且能区分「正常结束 / 用户取消 / 内部错误」。
"""

from __future__ import annotations

import threading
from typing import Any, Callable, TextIO

from ag_ui.core import EventType, RunErrorEvent, RunFinishedEvent

from aidev_agent.core.ag_ui.types import RunFinishedSuccessOutcome, serialize_run_finished_outcome
from aidev_agent.utils.event import RunId, stamp_round_end_event

from .protocol import encode_event

STATUS_FINISHED = "finished"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"


def build_cancel_terminal_events(thread_id: str) -> tuple[RunErrorEvent, RunFinishedEvent]:
    """构造与 ``streaming_helper._build_terminal_cancel_events`` 同构的取消终止事件对。"""
    error_event = RunErrorEvent(type=EventType.RUN_ERROR, message=RunId.CANCELLED_MESSAGE)
    finished_event = RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id=thread_id,
        run_id=RunId.CANCELLED,
        outcome=serialize_run_finished_outcome(RunFinishedSuccessOutcome()),
    )
    stamp_round_end_event(error_event)
    stamp_round_end_event(finished_event)
    return error_event, finished_event


def build_error_event(message: str) -> RunErrorEvent:
    """构造一条内部错误的终止事件。"""
    event = RunErrorEvent(type=EventType.RUN_ERROR, message=message)
    stamp_round_end_event(event)
    return event


def build_finished_event(thread_id: str, run_id: str) -> RunFinishedEvent:
    """构造一条正常结束的终止事件。"""
    event = RunFinishedEvent(
        type=EventType.RUN_FINISHED,
        thread_id=thread_id,
        run_id=run_id,
        outcome=serialize_run_finished_outcome(RunFinishedSuccessOutcome()),
    )
    stamp_round_end_event(event)
    return event


class SessionEventWriter:
    """会话级事件写出器：注入给 Agent 的 ``event_handler``。

    实例随会话存活，每轮 ``bind`` 一次请求 ``id``；``__call__`` 满足
    ``Callable[[BaseEvent], None]`` 契约，因此装配期既不会被误当成平台
    ``BaseSessionWriter``，也不依赖任何平台能力。
    """

    def __init__(
        self,
        stream: TextIO | None = None,
        *,
        lock: threading.Lock | None = None,
        sink: Callable[[Any], None] | None = None,
    ) -> None:
        self._stream = stream
        self._lock = lock or threading.Lock()
        self._sink = sink
        self.session_code: str = ""
        self.request_id: str = ""
        self.thread_id: str = ""
        self.assistant_text: list[str] = []
        self.saw_run_started = False
        self.saw_run_finished = False
        self.saw_cancel_error = False
        self.saw_cancelled_finished = False
        self.terminal_emitted = False
        self.status = STATUS_FINISHED
        self.error_message: str | None = None

    # ---------------- 生命周期 ----------------

    def bind(self, *, request_id: str, thread_id: str, sink: Callable[[Any], None] | None = None) -> None:
        """绑定本轮请求；同一会话串行执行，因此复用实例即可。"""
        self.request_id = request_id
        self.thread_id = thread_id
        self._sink = sink
        self.assistant_text = []
        self.saw_run_started = False
        self.saw_run_finished = False
        self.saw_cancel_error = False
        self.saw_cancelled_finished = False
        self.terminal_emitted = False
        self.status = STATUS_FINISHED
        self.error_message = None

    def set_session_code(self, session_code: str) -> None:
        self.session_code = session_code

    # ---------------- 事件入口 ----------------

    def __call__(self, event: Any) -> None:
        self._observe(event)
        self.emit(event)

    def emit(self, event: Any) -> None:
        """写出一条事件（不参与状态判定）。"""
        event_type = getattr(event, "type", None)
        if event_type in (EventType.RUN_FINISHED, EventType.RUN_ERROR):
            self.terminal_emitted = True
        if self._sink is not None:
            self._sink(event)
            return
        line = encode_event(self.request_id, event)
        with self._lock:
            if self._stream is not None:
                self._stream.write(line + "\n")
                self._stream.flush()

    def emit_error(self, message: str) -> None:
        """产出内部错误终止事件并标记本轮为 error。"""
        self.status = STATUS_ERROR
        self.error_message = message
        self.emit(build_error_event(message))

    def mark_cancelled(self) -> None:
        self.status = STATUS_CANCELLED

    def mark_error(self, message: str) -> None:
        self.status = STATUS_ERROR
        self.error_message = message

    def finalize(self) -> str:
        """补齐终止事件并返回本轮最终状态（``finished`` / ``cancelled`` / ``error``）。"""
        if self.status == STATUS_CANCELLED:
            cancel_error, cancel_finished = build_cancel_terminal_events(self.thread_id)
            if not self.saw_cancel_error:
                self.saw_cancel_error = True
                self.emit(cancel_error)
            if not self.saw_cancelled_finished:
                self.saw_cancelled_finished = True
                self.saw_run_finished = True
                self.emit(cancel_finished)
        elif self.status == STATUS_ERROR:
            if not self.terminal_emitted:
                self.emit(build_error_event(self.error_message or "执行失败"))
        elif not self.terminal_emitted:
            # 正常结束但缺少终止事件：补一条成功终止事件，保证「必须收尾」的契约
            self.saw_run_finished = True
            self.emit(build_finished_event(self.thread_id, self.request_id))
        return self.status

    # ---------------- 内部 ----------------

    def _observe(self, event: Any) -> None:
        event_type = getattr(event, "type", None)
        if event_type == EventType.RUN_STARTED:
            self.saw_run_started = True
        elif event_type == EventType.TEXT_MESSAGE_CONTENT:
            delta = getattr(event, "delta", "") or ""
            if delta:
                self.assistant_text.append(delta)
        elif event_type == EventType.RUN_ERROR:
            message = getattr(event, "message", "") or ""
            if message == RunId.CANCELLED_MESSAGE:
                self.saw_cancel_error = True
                self.status = STATUS_CANCELLED
            elif self.status != STATUS_CANCELLED:
                self.status = STATUS_ERROR
                self.error_message = message
        elif event_type == EventType.RUN_FINISHED:
            self.saw_run_finished = True
            if getattr(event, "run_id", None) == RunId.CANCELLED:
                self.saw_cancelled_finished = True
                self.status = STATUS_CANCELLED

    def take_assistant_text(self) -> str:
        return "".join(self.assistant_text)
