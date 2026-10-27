# -*- coding: utf-8 -*-
"""headless 会话模型：单进程多会话、同会话串行、跨会话并发（REQ-9 / REQ-10）。

每个 ``HeadlessSession`` 持有：
- 会话锁（同会话 ``turn`` 串行）；
- 会话级 ``MemorySaver`` checkpointer（``thread_id = session_code``）；
- 内存账本（``HeadlessLedger``，``build_type=SESSION`` 的历史来源）；
- 无平台 ``HeadlessResourceManager`` 与 ``SessionEventWriter``；
- 当前执行实例与取消信号。

实例一律经进程级注入点 ``agent_factory()`` 产出（``create`` / ``resume``），
仓内不存在绕过注册中心的构造路径（REQ-3）。
"""

from __future__ import annotations

import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, TextIO

from langgraph.checkpoint.base import BaseCheckpointSaver

from aidev_agent.enums import AgentBuildType
from aidev_agent.exceptions import AgentFactoryError
from aidev_agent.pydantic_models import ExecuteKwargs
from aidev_agent.services.agent.factory import agent_factory

from .errors import (
    AGENT_BUILD_FAILED,
    AGENT_INTERNAL_ERROR,
    RESUME_PAYLOAD_UNSUPPORTED,
    SESSION_BUSY_CANCELLED,
    SESSION_CLOSED,
    SESSION_NOT_FOUND,
    HeadlessError,
)
from .events import STATUS_ERROR, SessionEventWriter
from .resource_manager import HeadlessLedger, HeadlessResourceManager

logger = logging.getLogger("aidev-agent.headless")

#: 注入给 Agent 的渠道标识，仅用于 token/日志元数据，不产生平台上报
HEADLESS_CHANNEL_TYPE = "headless"

#: ``cancel`` 等待本轮收敛的上限；超时由入口补发取消终止事件并释放会话
CANCEL_TIMEOUT_SECONDS = 10.0

MessageSink = Callable[[Any], None]


class SessionConfig:
    """``create`` 请求解析出的会话配置（不含平台凭证）。"""

    def __init__(
        self,
        *,
        agent_code: str,
        model: str,
        base_url: str | None = None,
        auth_headers: dict[str, str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        username: str | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        event_sink: MessageSink | None = None,
    ) -> None:
        self.agent_code = agent_code
        self.model = model
        self.base_url = base_url
        self.auth_headers = auth_headers
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.username = username
        self.checkpointer = checkpointer
        self.event_sink = event_sink


class HeadlessSession:
    """单个宿主会话。"""

    def __init__(
        self,
        *,
        session_code: str,
        config: SessionConfig,
        stream: TextIO | None = None,
        write_lock: threading.Lock | None = None,
    ) -> None:
        self.session_code = session_code
        self.config = config
        self.ledger = HeadlessLedger()
        self.resource_manager = HeadlessResourceManager(
            agent_code=config.agent_code,
            chat_model=config.model,
            ledger=self.ledger,
            username=config.username,
        )
        self.checkpointer: BaseCheckpointSaver = config.checkpointer or _default_checkpointer()
        self.writer = SessionEventWriter(stream, lock=write_lock, sink=config.event_sink)
        self.writer.set_session_code(session_code)

        self._session_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._closed = False
        self._cancel_requested = False
        self._turn_active = False
        self._turn_finished = threading.Event()
        self._current_agent: Any = None
        self._pending_agent: Any = None

    # ---------------- 状态查询 ----------------

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def ensure_open(self) -> None:
        if self.closed:
            raise HeadlessError(SESSION_CLOSED, f"会话已关闭：{self.session_code}", {"session_code": self.session_code})

    # ---------------- create ----------------

    def prepare(self) -> None:
        """``create`` 阶段建立会话：经注入点 ``create`` 装配一次，装配期错误即时暴露。

        装配产物不参与后续轮次：每轮 ``turn`` 都会按当时的账本 / checkpointer 重新装配
        （``chat_history`` 在 build 期快照，复用会让第二轮读不到新历史）。
        """
        self._pending_agent = self._build_agent(build_type=AgentBuildType.SESSION, session_context_data=None)

    # ---------------- turn ----------------

    def turn(self, *, request_id: str, input_text: str = "", messages: list[dict] | None = None) -> dict:
        """驱动一轮对话，返回 ``{"session_code", "turn_id", "status"}``。"""
        self.ensure_open()
        stateless = bool(messages)
        return self._drive_turn(
            request_id=request_id,
            input_text=input_text,
            stateless=stateless,
            build=lambda: self._take_agent(
                build_type=AgentBuildType.DIRECT if stateless else AgentBuildType.SESSION,
                session_context_data=list(messages) if stateless else None,
                allow_pending=not stateless,
            ),
        )

    # ---------------- resume ----------------

    def resume(self, *, request_id: str, input_text: str = "", resume_payload: Any = None) -> dict:
        """同一会话续轮（不驱动中断恢复）。"""
        self.ensure_open()

        def _build():
            if _is_interrupt_payload(resume_payload):
                raise HeadlessError(
                    RESUME_PAYLOAD_UNSUPPORTED,
                    "headless 仅支持同一会话续轮，不支持审批 / ask_user_question 中断恢复",
                    {"fields": ["resume"]},
                )
            continuation = resume_payload if resume_payload is not None else {"input": input_text or ""}
            return self._build_agent_via_resume(continuation)

        return self._drive_turn(request_id=request_id, input_text=input_text, stateless=False, build=_build)

    # ---------------- 轮次执行（turn / resume 共用） ----------------

    def _drive_turn(self, *, request_id: str, input_text: str, stateless: bool, build) -> dict:
        """会话锁内串行执行一轮；事件 writer 的请求绑定也在锁内，避免轮次串扰。"""
        turn_id = uuid.uuid4().hex
        user_message_id = ""
        with self._session_lock:
            self._begin_turn(request_id)
            try:
                if not stateless:
                    # 与平台同序：本轮 user 记录先入账本，SESSION 装配时作为历史的一部分读到
                    user_message_id = self._append_user_record(input_text, turn_id)
                agent = self._build_guarded(build)
                execute_kwargs = ExecuteKwargs(
                    stream=True,
                    session_code=self.session_code,
                    thread_id=self.session_code,
                    turn_id=turn_id,
                    input=input_text or "",
                )
                self._run(agent, execute_kwargs)
                status = self._finish_turn()
                if not stateless:
                    self._append_assistant_record(turn_id)
                if status == STATUS_ERROR:
                    # 执行链只产出 RUN_ERROR 事件、未抛异常（如模型 5xx / 端点不可达）：
                    # 该轮本身是失败轮，响应必须 ok=false 并带稳定错误码（REQ-5 / REQ-7）。
                    # 事件侧不再补写 RUN_ERROR —— 基线序列（RUN_ERROR + 平台补发的 RUN_FINISHED）保持不变。
                    return self._result(
                        turn_id,
                        status,
                        HeadlessError(
                            AGENT_INTERNAL_ERROR,
                            self.writer.error_message or "本轮执行失败：执行链返回 RUN_ERROR",
                        ),
                    )
                return {"session_code": self.session_code, "turn_id": turn_id, "status": status}
            except HeadlessError as exc:
                if user_message_id:
                    self.ledger.drop(user_message_id)
                status = self._fail_turn(exc.message)
                return self._result(turn_id, status, exc)
            except AgentFactoryError as exc:
                if user_message_id:
                    self.ledger.drop(user_message_id)
                status = self._fail_turn(f"{exc.code}: {exc.reason}")
                return self._result(turn_id, status, _factory_error_to_headless(exc))
            except Exception as exc:  # pylint: disable=broad-except
                if user_message_id:
                    self.ledger.drop(user_message_id)
                logger.exception("headless turn failed: session_code=%s", self.session_code)
                status = self._fail_turn(f"{type(exc).__name__}: {exc}")
                return self._result(
                    turn_id, status, HeadlessError(AGENT_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                )
            finally:
                self._end_turn(request_id)

    # ---------------- cancel ----------------

    def cancel(self) -> dict:
        """请求取消当前轮，等待其以取消终止事件收尾（上限 ``CANCEL_TIMEOUT_SECONDS``）。"""
        self.ensure_open()
        with self._state_lock:
            if not self._turn_active:
                return {"session_code": self.session_code, "cancelled": True}
            if self._cancel_requested:
                raise HeadlessError(
                    SESSION_BUSY_CANCELLED,
                    "前一轮仍在取消收尾",
                    {"session_code": self.session_code},
                )
            self._cancel_requested = True
            agent = self._current_agent

        self.writer.mark_cancelled()
        if agent is not None:
            try:
                agent.stop()
            except Exception:  # pylint: disable=broad-except
                logger.warning("headless cancel: agent.stop failed: session_code=%s", self.session_code, exc_info=True)

        if not self._turn_finished.wait(timeout=CANCEL_TIMEOUT_SECONDS):
            # 收敛超时：入口侧补发取消终止事件，保证有界时间内可观测
            logger.warning("headless cancel timeout: session_code=%s", self.session_code)
            self.writer.finalize()
        return {"session_code": self.session_code, "cancelled": True}

    # ---------------- close ----------------

    def close(self) -> dict:
        """释放实例、checkpointer、账本与线程句柄。"""
        with self._state_lock:
            already_closed = self._closed
            self._closed = True
            agent = self._current_agent
            self._current_agent = None
            self._pending_agent = None
        if agent is not None:
            try:
                agent.stop()
            except Exception:  # pylint: disable=broad-except
                logger.warning("headless close: agent.stop failed: session_code=%s", self.session_code, exc_info=True)
        self.ledger.clear()
        self.checkpointer = None
        return {"session_code": self.session_code, "closed": True, "already_closed": already_closed}

    # ---------------- 内部：装配 ----------------

    def _build_agent(self, *, build_type: AgentBuildType, session_context_data: list[dict] | None):
        return agent_factory().create(
            agent_code=self.config.agent_code,
            build_type=build_type,
            session_code=self.session_code,
            session_context_data=session_context_data,
            resource_manager=self.resource_manager,
            checkpointer=self.checkpointer,
            event_handler=self.writer,
            username=self.config.username,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            auth_headers=self.config.auth_headers,
            channel_type=HEADLESS_CHANNEL_TYPE,
        )

    def _build_agent_via_resume(self, payload: Any):
        return agent_factory().resume(
            session_code=self.session_code,
            resume=payload,
            agent_code=self.config.agent_code,
            build_type=AgentBuildType.SESSION,
            resource_manager=self.resource_manager,
            checkpointer=self.checkpointer,
            event_handler=self.writer,
            username=self.config.username,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            auth_headers=self.config.auth_headers,
            channel_type=HEADLESS_CHANNEL_TYPE,
        )

    @staticmethod
    def _build_guarded(builder):
        """装配期异常归一为 ``agent.build_failed``；注入点/headless 的结构化错误原样上抛。"""
        try:
            return builder()
        except (HeadlessError, AgentFactoryError):
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise HeadlessError(AGENT_BUILD_FAILED, f"{type(exc).__name__}: {exc}") from exc

    def _take_agent(self, *, build_type: AgentBuildType, session_context_data, allow_pending: bool):
        """每轮重新装配（历史在 build 期快照，复用实例会读到过期历史）。"""
        with self._state_lock:
            self._pending_agent = None
        return self._build_agent(build_type=build_type, session_context_data=session_context_data)

    # ---------------- 内部：执行 ----------------

    def _run(self, agent: Any, execute_kwargs: ExecuteKwargs) -> None:
        with self._state_lock:
            self._current_agent = agent
        result = agent.execute(execute_kwargs)
        if isinstance(result, str):
            return
        for _chunk in result:  # 事件经 event_handler（SessionEventWriter）写出
            pass

    def _begin_turn(self, request_id: str) -> None:
        with self._state_lock:
            self._turn_active = True
            self._cancel_requested = False
            self._turn_finished.clear()
        self.writer.bind(request_id=request_id, thread_id=self.session_code, sink=self.config.event_sink)
        if self.config.base_url:
            from aidev_agent.config import settings

            settings.set("LLM_GW_ENDPOINT", self.config.base_url)

    def _end_turn(self, request_id: str) -> None:
        with self._state_lock:
            self._turn_active = False
            self._current_agent = None
            self._cancel_requested = False
        self._turn_finished.set()

    def _finish_turn(self) -> str:
        with self._state_lock:
            cancelled = self._cancel_requested
        if cancelled:
            self.writer.mark_cancelled()
        return self.writer.finalize()

    def _fail_turn(self, message: str) -> str:
        self.writer.emit_error(message)
        return self.writer.status

    def _append_user_record(self, input_text: str, turn_id: str) -> str:
        """把本轮 user 记录写入账本（返回 message_id，供失败回滚）。"""
        if not input_text:
            return ""
        message_id = uuid.uuid4().hex
        self.ledger.append(
            {
                "role": "user",
                "content": input_text,
                "message_id": message_id,
                "turn_id": turn_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "success",
                "property": {"extra": {}},
            }
        )
        return message_id

    def _append_assistant_record(self, turn_id: str) -> None:
        answer = self.writer.take_assistant_text()
        now = datetime.now(timezone.utc).isoformat()
        if answer:
            self.ledger.append(
                {
                    "role": "assistant",
                    "content": answer,
                    "message_id": uuid.uuid4().hex,
                    "turn_id": turn_id,
                    "created_at": now,
                    "status": "success",
                    "property": {"extra": {}},
                }
            )

    def _result(self, turn_id: str, status: str, error: HeadlessError) -> dict:
        return {
            "session_code": self.session_code,
            "turn_id": turn_id,
            "status": status,
            "error": error.to_payload(),
        }


class SessionRegistry:
    """``session_code -> HeadlessSession`` 注册表（会话标识由调用方提供）。"""

    def __init__(self, *, stream: TextIO | None = None, write_lock: threading.Lock | None = None) -> None:
        self._sessions: dict[str, HeadlessSession] = {}
        self._lock = threading.Lock()
        self._stream = stream
        self._write_lock = write_lock or threading.Lock()

    def create(self, *, session_code: str, config: SessionConfig) -> HeadlessSession:
        session = HeadlessSession(
            session_code=session_code,
            config=config,
            stream=self._stream,
            write_lock=self._write_lock,
        )
        with self._lock:
            self._sessions[session_code] = session
        return session

    def get(self, session_code: str) -> HeadlessSession:
        with self._lock:
            session = self._sessions.get(session_code)
        if session is None:
            raise HeadlessError(SESSION_NOT_FOUND, f"未知会话：{session_code}", {"session_code": session_code})
        return session

    def remove(self, session_code: str) -> None:
        with self._lock:
            self._sessions.pop(session_code, None)

    def codes(self) -> list[str]:
        with self._lock:
            return list(self._sessions)


def _default_checkpointer() -> BaseCheckpointSaver:
    from langgraph.checkpoint.memory import MemorySaver

    return MemorySaver()


def _is_interrupt_payload(payload: Any) -> bool:
    """判断负载是否为 bkai 中断恢复形状（headless 不驱动）。"""
    items = payload if isinstance(payload, list) else [payload]
    for item in items:
        if isinstance(item, dict) and ("interruptId" in item or "status" in item or "payload" in item):
            return True
    return False


def _factory_error_to_headless(exc: AgentFactoryError) -> HeadlessError:
    return HeadlessError(str(exc.code or "agent.unsupported"), getattr(exc, "reason", None) or exc.message)


__all__ = [
    "CANCEL_TIMEOUT_SECONDS",
    "HeadlessSession",
    "SessionConfig",
    "SessionRegistry",
]
