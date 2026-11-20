# -*- coding: utf-8 -*-
"""SDK event observation, terminal semantics, and real platform writer integration."""

from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import Any, Callable
from uuid import uuid4

from ag_ui.core import Event, EventType, RunErrorEvent, RunFinishedEvent
from aidev_agent.core.ag_ui.types import serialize_run_finished_outcome
from aidev_agent.services.event_handlers.agui_writer import AGUISessionWriter
from aidev_agent.services.event_handlers.base import BaseSessionWriter
from aidev_agent.utils.event import RunId
from pydantic import TypeAdapter

from .. import terminal

logger = getLogger(__name__)


STATUS_FINISHED = "finished"
STATUS_INTERRUPT = "interrupt"
STATUS_CANCELLED = "cancelled"
STATUS_ERROR = "error"


# 终止事件的**唯一**形状定义在纯 stdlib 的 ``runtime.terminal``（父进程要产出同形事件，
# 而父进程不得装载 SDK）。SDK 侧的 builder 只做模型转换：形状来自那一处，不在这里重复。
def build_cancel_terminal_events(thread_id: str) -> tuple[RunErrorEvent, RunFinishedEvent]:
    """构造与 ``streaming_helper._build_terminal_cancel_events`` 同构的取消终止事件对。"""
    error_event, finished_event = terminal.build_cancel_terminal_events(thread_id)

    return RunErrorEvent(**error_event), RunFinishedEvent(**finished_event)


def build_error_event(message: str) -> RunErrorEvent:
    """构造一条内部错误的终止事件。"""
    return RunErrorEvent(**terminal.build_error_event(message))


def build_finished_event(thread_id: str, run_id: str) -> RunFinishedEvent:
    """构造一条正常结束的终止事件。"""
    return RunFinishedEvent(**terminal.build_finished_event(thread_id, run_id))


class SessionEventWriter:
    """会话级事件写出器：注入给 Agent 的 ``event_handler``。

    实例随会话存活，每轮 ``bind`` 一次请求 ``id``；``__call__`` 满足
    ``Callable[[BaseEvent], None]`` 契约，因此装配期既不会被误当成平台
    ``BaseSessionWriter``，也不依赖任何平台能力。
    """

    def __init__(self) -> None:
        self._sink = None
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
        #: 中断轮回传的 pending 中断（``RUN_FINISHED.outcome.interrupts``）
        self.interrupts: list[dict] = []

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
        self.interrupts = []

    def set_session_code(self, session_code: str) -> None:
        self.session_code = session_code

    # ---------------- 事件入口 ----------------

    def __call__(self, event: Any) -> None:
        event = _coerce_event(event)
        self._observe(event)
        self.emit(event)

    def emit(self, event: Any) -> None:
        """写出一条事件（不参与状态判定）。"""
        event_type = _field(event, "type")
        if event_type in (EventType.RUN_FINISHED, EventType.RUN_ERROR):
            self.terminal_emitted = True
        if self._sink is not None:
            self._sink(event)
            return

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
        event_type = _field(event, "type")
        if event_type == EventType.RUN_STARTED:
            self.saw_run_started = True
        elif event_type == EventType.TEXT_MESSAGE_CONTENT:
            delta = _field(event, "delta") or ""
            if delta:
                self.assistant_text.append(delta)
        elif event_type == EventType.RUN_ERROR:
            message = _field(event, "message") or ""
            if message == RunId.CANCELLED_MESSAGE:
                self.saw_cancel_error = True
                self.status = STATUS_CANCELLED
            elif self.status != STATUS_CANCELLED:
                self.status = STATUS_ERROR
                self.error_message = message
        elif event_type == EventType.RUN_FINISHED:
            self.saw_run_finished = True
            if _field(event, "run_id", "runId") == RunId.CANCELLED:
                self.saw_cancelled_finished = True
                self.status = STATUS_CANCELLED
                return
            # ag-ui 的 ``RunFinishedEvent`` 会把 outcome 归一成 ``RunFinishedInterruptOutcome``
            # 模型对象（不再是 dict），所以先按 SDK 的序列化器转回契约字典再判定。
            outcome = _field(event, "outcome")
            outcome = outcome if isinstance(event, dict) else serialize_run_finished_outcome(outcome)
            if (
                self.status not in (STATUS_ERROR, STATUS_CANCELLED)
                and isinstance(outcome, dict)
                and outcome.get("type") == "interrupt"
            ):
                # 中断轮不是 finished / error：单独一态，响应 ok=true 并回传中断（REQ-17）
                self.status = STATUS_INTERRUPT
                interrupts = outcome.get("interrupts") or []
                self.interrupts = [item for item in interrupts if isinstance(item, dict)]

    def take_assistant_text(self) -> str:
        return "".join(self.assistant_text)


def _field(event: Any, *names: str) -> Any:
    """读事件字段：模型对象读属性，dict 载荷读键（依次尝试别名）。"""
    for name in names:
        value = event.get(name) if isinstance(event, dict) else getattr(event, name, None)
        if value is not None:
            return value
    return None


def _coerce_event(event: Any) -> Any:
    """AG-UI 事件的 dict 形态（JSON 载荷）转回模型对象；无法识别的原样返回。

    ``try`` 只包**校验调用**：它保护的是"这个 dict 不是合法 AG-UI 事件"，不是导入失败。
    """
    if not isinstance(event, dict) or "type" not in event:
        return event
    try:
        return TypeAdapter(Event).validate_python(event)
    except Exception:  # noqa: BLE001 - 不认识就原样转发
        return event


#: 只发给平台回写 writer、不进入 SSE 的 CUSTOM 事件（与 SDK 的 SSE 路径保持同一可见集合）。
_PERSISTENCE_ONLY_NAMES = frozenset({"aidev_session_chat_model_end", "user_input_saved", "ask_user_question_finalized"})


def platform_session_writer(*, session_code: str, client: Any, username: str, forward: Any) -> Any:
    """平台来源的事件处理器：SDK 的平台回写 writer（持久化会话内容）+ 把同一事件转发给会话 writer。

    转发让三个协议走同一个事件出口（:class:`SessionEventWriter` → 本轮 sink），而平台回写
    保留 ``_prepare_pre_run_history`` 所需的 ``BaseSessionWriter`` 身份。
    """

    class _ForwardingSessionWriter(AGUISessionWriter):
        def __call__(self, event: Any) -> None:
            super().__call__(event)
            if str(getattr(event, "name", "") or "") in _PERSISTENCE_ONLY_NAMES:
                return
            forward(event)

    return _ForwardingSessionWriter(session_code=session_code, client=client, username=username)


def local_session_writer(*, session_code: str, username: str, ledger: Any, forward: Any, turn_id: str) -> Any:
    """A real SDK BaseSessionWriter whose persistence target is the worker ledger."""

    class LocalSessionWriter(BaseSessionWriter):
        def _do_create_content(self, payload: dict[str, Any], headers: dict[str, str]) -> int:
            record = dict(payload)
            properties = record.get("property") or {}
            extra = properties.get("extra") or {}
            record["turn_id"] = properties.get("turn_id") or self.turn_id
            record["message_id"] = extra.get("message_id") or uuid4().hex
            record["created_at"] = datetime.now(timezone.utc).isoformat()
            records = ledger.records()
            # The pre-build user row is replaced by the SDK's authoritative input event.
            for previous in records:
                if (
                    record.get("role") == "user"
                    and previous.get("role") == "user"
                    and (
                        previous.get("turn_id") == record["turn_id"]
                        and previous.get("content") == record.get("content")
                    )
                ):
                    ledger.drop(previous["message_id"])
            record["id"] = max((item.get("id", 0) for item in records), default=0) + 1
            ledger.append(record)
            return record["id"]

        def _do_update_content(self, content_id: int, payload: dict[str, Any], headers: dict[str, str]) -> None:
            for previous in ledger.records():
                if previous.get("id") == content_id:
                    ledger.drop(previous["message_id"])
                    previous.update(payload)
                    ledger.append(previous)
                    return

        def __call__(self, event: Any) -> None:
            event = _coerce_event(event)
            if hasattr(event, "type"):
                super().__call__(event)
            if _field(event, "name") not in _PERSISTENCE_ONLY_NAMES:
                forward(event)

    return LocalSessionWriter(session_code=session_code, username=username, turn_id=turn_id)
