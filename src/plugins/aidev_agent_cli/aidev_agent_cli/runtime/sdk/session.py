"""Worker-local SDK turn semantics. Agents, writers, hooks and savers never cross spawn."""

from __future__ import annotations

import asyncio
import os
import threading
import uuid
from datetime import datetime, timezone
from logging import getLogger
from typing import Any, Callable

from aidev_agent.enums import AgentBuildType
from aidev_agent.exceptions import AgentFactoryError
from aidev_agent.packages.langchain_core.models.llm_retry import RetryState
from aidev_agent.pydantic_models import ExecuteKwargs

from .checkpoints import CheckpointStore
from .events import (
    STATUS_CANCELLED,
    STATUS_ERROR,
    STATUS_INTERRUPT,
    SessionEventWriter,
    local_session_writer,
    platform_session_writer,
)
from .factory import build_agent as build_session_agent
from .factory import ensure_session
from .hooks import HeadlessHookBus, build_hook_set
from .interrupts import ITSM_APPROVAL_REASON, RESUMABLE_REASONS, HeadlessInterruptProcessor, match_pending_interrupt
from ..auth import AuthManager, ProcessConfig
from ..contracts import RunOutcome
from ..errors import RuntimeFault
from ...infrastructure.errors import (
    AGENT_BUILD_FAILED,
    AGENT_INTERNAL_ERROR,
    RESUME_INTERRUPT_NOT_FOUND,
    RESUME_INTERRUPT_UNSUPPORTED,
    RESUME_PAYLOAD_UNSUPPORTED,
    SESSION_CLOSED,
    SESSION_PENDING_INTERRUPT,
)
from ...resources.loaders.base import ResourceOverrides
from ...resources.manager import HeadlessLedger, LocalResourceManager
from ...resources.sources import uses_platform

logger = getLogger(__name__)

HEADLESS_CHANNEL_TYPE = "headless"


class SDKSession:
    """Worker-local SDK executor. One instance is created and closed for one run."""

    def __init__(
        self,
        *,
        session_code: str = "",
        config: ProcessConfig | None = None,
        agent_code: str = "",
        resources: tuple[str, ...] = (),
        auth: AuthManager | None = None,
        checkpoints: CheckpointStore | None = None,
        model: str = "",
        username: str = "",
        base_url: str = "",
        auth_headers: dict[str, str] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        hooks: Any = None,
        tools: list | None = None,
        ledger: HeadlessLedger | None = None,
        checkpointer: Any = None,
        hook_channel: Any = None,
    ) -> None:
        self.session_code = session_code

        self.config = config or ProcessConfig()

        self.checkpoints = checkpoints or CheckpointStore()
        self._owns_checkpoints = checkpoints is None

        self.auth = auth or AuthManager()

        self.resources: tuple[str, ...] = tuple(resources)

        self.agent_code: str = agent_code

        self._creds: dict[str, str] | None = None

        self._resource_manager: Any | None = None

        self.current_model: str | None = model or None

        self.username: str = username

        self.base_url: str = base_url
        self.auth_headers: dict[str, str] | None = auth_headers
        self.temperature: float | None = temperature
        self.max_tokens: int | None = max_tokens

        self.hooks = hooks
        self.tools: list | None = list(tools) if tools else None

        if ledger is None and not uses_platform(self.resources):
            ledger = HeadlessLedger()
        self.ledger: HeadlessLedger | None = ledger

        self.checkpointer = checkpointer

        self._known_sessions: set[str] = set()

        self.writer = SessionEventWriter()
        self.writer.set_session_code(self.session_code)

        self._state_lock = threading.Lock()
        self._closed = False
        self._cancel_requested = False

        self._cancel_token: Any = None
        self._current_agent: Any = None

        self.hook_bus: HeadlessHookBus | None = None
        if self.hooks is not None:
            if hook_channel is None:
                raise ValueError("enabled hooks require a typed worker request channel")
            self.hook_bus = HeadlessHookBus(
                config=self.hooks,
                session_code=self.session_code,
                channel=hook_channel,
                writer=self.writer,
                cancel_check=self._cancel_requested_now,
            )

        self._turn_count = 0

        self._pending_interrupts: list[dict] = []

    @property
    def closed(self) -> bool:
        with self._state_lock:
            return self._closed

    def ensure_open(self) -> None:
        if self.closed:
            raise RuntimeFault(SESSION_CLOSED, f"会话已关闭：{self.session_code}", {"session_code": self.session_code})

    def prepare(self) -> None:
        """Validate real SDK assembly; close releases the resulting worker-owned resources."""
        self.ensure_platform_session()
        self._current_agent = self._build_agent(build_type=AgentBuildType.SESSION, session_context_data=None)

    def turn(
        self,
        *,
        request_id: str,
        input_text: str = "",
        messages: list[dict] | None = None,
        context_mode: bool = False,
        execute_kwargs: dict[str, Any] | None = None,
        sink: Any = None,
    ) -> RunOutcome:
        """Execute a typed chat or context turn after SDK-boundary normalization."""
        carried = dict(execute_kwargs or {})
        self.ensure_open()
        if self._pending_interrupts and not context_mode:
            raise RuntimeFault(
                SESSION_PENDING_INTERRUPT,
                "会话存在未恢复的中断，请先 resume 再开启新一轮",
                {"session_code": self.session_code, "interrupts": self._pending_interrupts[:1]},
            )
        stateless = context_mode

        def _build(hook_handlers: Any):
            return self._build_agent(
                build_type=AgentBuildType.DIRECT if stateless else AgentBuildType.SESSION,
                session_context_data=list(messages or []) if stateless else None,
                hook_handlers=hook_handlers,
            )

        return self._drive_turn(
            request_id=request_id,
            input_text=input_text,
            stateless=stateless,
            build=_build,
            resume_payload=None,
            carried=carried,
            sink=sink,
        )

    def resume(self, *, request_id: str, resume_payload: Any, input_text: str = "", sink: Any = None) -> RunOutcome:
        """Resume one pending interrupt; ordinary continuation is a chat turn."""
        self.ensure_open()

        def build(hook_handlers):
            item = self._validate_resume_payload(resume_payload)
            return self._build_agent_via_resume(
                item, hook_handlers=hook_handlers, interrupt_processor=HeadlessInterruptProcessor(resume_item=item)
            )

        return self._drive_turn(
            request_id=request_id,
            input_text=input_text,
            stateless=False,
            build=build,
            resume_payload=resume_payload,
            carried={},
            sink=sink,
        )

    def _drive_turn(
        self,
        *,
        request_id: str,
        input_text: str,
        stateless: bool,
        build: Callable[[Any], Any],
        resume_payload: Any = None,
        carried: dict[str, Any] | None = None,
        sink: Any = None,
    ) -> RunOutcome:
        """Execute and finalize one SDK turn; the parent publishes terminal events only after join."""
        turn_id = uuid.uuid4().hex
        user_message_id = ""
        keep_ask_memo = False
        self._begin_turn(request_id, turn_id, sink)
        try:
            hook_handlers = self._hook_handlers(turn_id, input_text)

            self.ensure_platform_session()
            agent = self._build_guarded(lambda: build(hook_handlers))
            execute_kwargs = self._execute_kwargs(
                turn_id=turn_id, input_text=input_text, resume_payload=resume_payload, carried=carried
            )
            self._run(agent, execute_kwargs)
            if (
                not stateless
                and self.ledger is not None
                and not any(
                    record.get("role") == "user" and record.get("turn_id") == turn_id
                    for record in self.ledger.records()
                )
            ):
                user_message_id = self._append_user_record(input_text, turn_id)

            status = self._finish_turn()
            if not stateless and self.ledger is not None:
                self._append_assistant_record(turn_id)
            if status == STATUS_INTERRUPT:
                keep_ask_memo = True
                self._pending_interrupts = list(self.writer.interrupts)

                return RunOutcome(
                    status="interrupted",
                    session_code=self.session_code,
                    turn_id=turn_id,
                    interrupts=list(self._pending_interrupts),
                )
            self._pending_interrupts = []
            if status == STATUS_ERROR:
                return RunOutcome(
                    status="failed",
                    session_code=self.session_code,
                    turn_id=turn_id,
                    error_code=AGENT_INTERNAL_ERROR,
                    error_message=self.writer.error_message or "本轮执行失败：执行链返回 RUN_ERROR",
                )

            return RunOutcome(
                status="cancelled" if status == STATUS_CANCELLED else "finished",
                session_code=self.session_code,
                turn_id=turn_id,
            )
        except RuntimeFault as exc:
            if user_message_id and self.ledger is not None:
                self.ledger.drop(user_message_id)
            status = self._fail_turn(exc.message)

            return RunOutcome(
                status="failed",
                session_code=self.session_code,
                turn_id=turn_id,
                error_code=exc.code,
                error_message=exc.message,
            )
        except AgentFactoryError as exc:
            if user_message_id and self.ledger is not None:
                self.ledger.drop(user_message_id)
            status = self._fail_turn(f"{exc.code}: {exc.reason}")
            mapped = _factory_error_to_headless(exc)

            return RunOutcome(
                status="failed",
                session_code=self.session_code,
                turn_id=turn_id,
                error_code=mapped.code,
                error_message=mapped.message,
            )
        except Exception as exc:  # pylint: disable=broad-except
            if user_message_id and self.ledger is not None:
                self.ledger.drop(user_message_id)
            logger.exception("headless turn failed: session_code=%s", self.session_code)
            status = self._fail_turn(f"{type(exc).__name__}: {exc}")

            return RunOutcome(
                status="failed",
                session_code=self.session_code,
                turn_id=turn_id,
                error_code=AGENT_INTERNAL_ERROR,
                error_message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._end_turn(request_id)

            if self.hook_bus is not None and not keep_ask_memo:
                self.hook_bus.reset_turn()

    def close(self) -> None:
        """Release worker-local SDK resources and owned checkpoint connections."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            agent = self._current_agent
            self._current_agent = None
        try:
            if agent is not None:
                agent.stop()
        finally:
            if self.ledger is not None:
                self.ledger.clear()
            self.checkpointer = None
            if self._owns_checkpoints:
                self.checkpoints.close()

    def _build_agent(
        self, *, build_type: AgentBuildType, session_context_data: list[dict] | None, hook_handlers: Any = None
    ):
        return self.build_agent(
            build_type=build_type,
            session_context_data=session_context_data,
            checkpointer=self._turn_checkpointer(),
            event_handler=self._event_handler(),
            channel_type=HEADLESS_CHANNEL_TYPE,
            hook_handlers=hook_handlers,
            interrupt_processor=HeadlessInterruptProcessor(),
        )

    def _build_agent_via_resume(
        self,
        payload: Any,
        *,
        hook_handlers: Any = None,
        interrupt_processor: Any = None,
    ):
        return self.build_agent(
            resume=payload,
            build_type=AgentBuildType.SESSION,
            checkpointer=self._turn_checkpointer(),
            event_handler=self._event_handler(),
            channel_type=HEADLESS_CHANNEL_TYPE,
            hook_handlers=hook_handlers,
            interrupt_processor=interrupt_processor,
        )

    @staticmethod
    def _build_guarded(builder):
        """Normalize real SDK assembly failures; parent ProcessWorker bounds build duration."""
        try:
            return builder()
        except (RuntimeFault, AgentFactoryError):
            raise
        except Exception as exc:  # pylint: disable=broad-except
            raise RuntimeFault(AGENT_BUILD_FAILED, f"{type(exc).__name__}: {exc}") from exc

    def _run(self, agent: Any, execute_kwargs: ExecuteKwargs) -> None:
        """Drain the synchronous SDK generator inside the spawned worker, including retry handling."""
        with self._state_lock:
            self._current_agent = agent
        handler = getattr(agent, "event_handler", None)
        if handler is not None and hasattr(handler, "cancel_checker"):
            try:
                handler.cancel_checker = self._cancel_requested_now
            except Exception:  # noqa: BLE001 - 可选钩子，永不致命
                logger.info("event handler rejected cancel_checker; relying on agent.stop")

        retries = int(os.environ.get("AGENT_TUI_LLM_RATE_LIMIT_RETRIES", "0") or 0)
        if retries <= 0:
            _drain(agent.execute(execute_kwargs))
            return

        def report_retry(event: dict) -> None:
            if self._cancel_requested_now():
                retry_state.cancel()
            self.writer.emit(event)

        retry_state = RetryState(
            max_retries=retries,
            wait_budget=float(os.environ.get("AGENT_TUI_LLM_RATE_LIMIT_WAIT_BUDGET", "0") or 0),
            cancel_checker=self._cancel_requested_now,
            on_event=report_retry,
        )
        try:
            with retry_state.bind():
                _drain(agent.execute(execute_kwargs))
        except asyncio.CancelledError:
            if not self._cancel_requested_now():
                raise
            with self._state_lock:
                self._cancel_requested = True
        finally:
            retry_state.cancel()

    def _execute_kwargs(
        self, *, turn_id: str, input_text: str, resume_payload: Any, carried: dict[str, Any] | None
    ) -> ExecuteKwargs:
        """本轮执行参数：三个协议同一组基础键；平台来源追加持久化输入；调用方透传的覆盖在最后。"""
        values: dict[str, Any] = {
            "stream": True,
            "session_code": self.session_code,
            "thread_id": self.session_code,
            "turn_id": turn_id,
            "input": input_text or "",
            "resume": resume_payload,
        }
        if self._uses_platform():
            values.update({"stream_mode": "start", "persist_input": True})
        values.update({key: value for key, value in (carried or {}).items() if key != "resume"})
        return ExecuteKwargs.model_validate(values)

    def _event_handler(self) -> Any:
        """本轮的 SDK 事件处理器：无平台时就是会话 writer；平台来源时是「平台回写 + 转发」组合 writer。"""
        if not self._uses_platform():
            return local_session_writer(
                session_code=self.session_code,
                username=self.username or self.auth.username,
                ledger=self.ledger,
                forward=self.writer,
                turn_id=getattr(self, "_current_turn_id", ""),
            )
        rm = self.ensure_manager()
        return platform_session_writer(
            session_code=self.session_code,
            client=rm.get_client(),
            username=self.username or getattr(rm, "username", ""),
            forward=self.writer,
        )

    def _uses_platform(self) -> bool:
        return uses_platform(self.resources)

    def _turn_checkpointer(self) -> Any:
        """本轮 saver：调用方显式给的优先；否则一律用进程级 checkpoint 存储（三协议一致）。"""
        return self.checkpointer if self.checkpointer is not None else self.checkpoints.ensure_sync()

    def _begin_turn(self, request_id: str, turn_id: str, sink: Any = None) -> None:
        self._current_turn_id = turn_id
        with self._state_lock:
            self._cancel_requested = False
        self.writer.bind(request_id=request_id, thread_id=self.session_code, sink=sink)
        if self.hook_bus is not None:
            self.hook_bus.bind(request_id=request_id, turn_id=turn_id)

    def _end_turn(self, request_id: str) -> None:
        with self._state_lock:
            self._cancel_requested = False
        self._turn_count += 1

    def _hook_handlers(self, turn_id: str, input_text: str) -> Any:
        """本轮注入 SDK 的 ``HookSet``；未配置 hooks 或点位全关时为 None（零回归）。"""
        if self.hook_bus is None:
            return None
        return build_hook_set(
            self.hook_bus, turn_id=turn_id, input_text=input_text or "", first_turn=self._turn_count == 0
        )

    def bind_cancel_token(self, token: Any) -> None:
        """Bind the worker control channel cancellation checker."""
        with self._state_lock:
            self._cancel_token = token

    def request_stop(self) -> None:
        """Stop the worker-local SDK agent cooperatively; parent ProcessWorker enforces kill/join."""
        with self._state_lock:
            self._cancel_requested = True
            agent = self._current_agent

        self.writer.mark_cancelled()
        if agent is not None:
            try:
                agent.stop()
            except Exception:  # pylint: disable=broad-except
                logger.warning("headless cancel: agent.stop failed: session_code=%s", self.session_code, exc_info=True)

    def _cancel_requested_now(self) -> bool:
        with self._state_lock:
            if self._cancel_requested:
                return True
            token = self._cancel_token

        return bool(token is not None and token.is_cancelled())

    def _validate_resume_payload(self, payload: Any) -> dict:
        """校验中断恢复负载命中该会话 pending 中断（REQ-17 / ACC-21）。"""
        items = payload if isinstance(payload, list) else [payload]
        if not items:
            raise RuntimeFault(
                RESUME_PAYLOAD_UNSUPPORTED,
                "恢复负载为空：中断恢复需要 interruptId",
                {"fields": ["resume"]},
            )
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("interruptId"), str) or not item["interruptId"]:
                raise RuntimeFault(
                    RESUME_PAYLOAD_UNSUPPORTED,
                    "恢复负载形状不支持：中断恢复项需包含非空字符串 interruptId",
                    {"fields": ["resume.interruptId"]},
                )
        if len(items) > 1:
            raise RuntimeFault(
                RESUME_PAYLOAD_UNSUPPORTED,
                "一次 resume 只能恢复一个中断，请逐次恢复",
                {"fields": ["resume"], "count": len(items)},
            )

        item = items[0]
        if not self._pending_interrupts and self._uses_platform():
            return item
        if not self._pending_interrupts:
            raise RuntimeFault(
                RESUME_INTERRUPT_NOT_FOUND,
                "该会话当前没有待恢复的中断",
                {"interruptId": item["interruptId"]},
            )
        matched = match_pending_interrupt(self._pending_interrupts, item)
        if matched is None:
            raise RuntimeFault(
                RESUME_INTERRUPT_NOT_FOUND,
                "interruptId 未命中该会话的 pending 中断",
                {"fields": ["resume.interruptId"]},
            )
        reason = matched.get("reason")
        if reason == ITSM_APPROVAL_REASON:
            raise RuntimeFault(
                RESUME_INTERRUPT_UNSUPPORTED,
                "headless 本期不接线 ITSM 权限审批中断的恢复，请由平台侧完成审批",
                {"reason": reason},
            )
        if reason not in RESUMABLE_REASONS:
            raise RuntimeFault(
                RESUME_INTERRUPT_NOT_FOUND,
                "该中断类型不在 headless 可恢复范围内",
                {"reason": reason},
            )
        return item

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
        if answer and not any(
            record.get("role") == "assistant" and record.get("turn_id") == turn_id and record.get("content") == answer
            for record in self.ledger.records()
        ):
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

    def build_agent(self, **params: Any) -> Any:
        """Assemble an SDK agent with worker-owned resources, saver, tools and real session writer."""
        rm = self.ensure_manager()
        params.setdefault("checkpointer", self.checkpointer)

        return build_session_agent(
            rm,
            self.session_code,
            self._resolve_agent_code(rm),
            self.username or None,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            auth_headers=self.auth_headers,
            base_url=self.base_url or None,
            injected_tools=self.tools,
            **params,
        )

    def ensure_platform_session(self, session_code: str | None = None) -> None:
        """带平台来源时，让平台知晓本会话（幂等；无平台来源时什么也不做）。

        SESSION 模式下 SDK 会向平台拉取会话历史，未知的 code 会 404。
        """
        code = session_code or self.session_code
        rm = self.ensure_manager()
        if code in self._known_sessions or not getattr(rm, "uses_platform", True):
            return
        ensure_session(rm, code)
        self._known_sessions.add(code)

    def ensure_manager(self) -> LocalResourceManager:
        """Build worker-owned resources from the accepted identity and explicit overrides."""
        if self._resource_manager is None:
            creds = self.config.credentials() if uses_platform(self.resources) else {}
            self._creds = creds
            agent_code = self._resolve_agent_code(None)
            self._resource_manager = LocalResourceManager.build(
                creds,
                self.username or self.auth.username,
                access_token=self.auth.access_token,
                overrides=ResourceOverrides(model=self.current_model or "", agent_code=agent_code),
                sources=self.resources,
                agent_code=agent_code,
                model=self.current_model or "",
                ledger=self.ledger,
            )
        return self._resource_manager

    def _resolve_agent_code(self, rm: LocalResourceManager | None) -> str:
        """Use the accepted agent selection, then the worker's platform default."""
        return (
            self.agent_code
            or (self._creds or {}).get("BKPAAS_APP_CODE", "")
            or (rm.get_agent_code() if rm is not None else "")
        )

    def describe_agent_config(self) -> dict[str, Any]:
        """Return the manager projection inside the worker."""
        rm = self.ensure_manager()

        return rm.describe_agent_config(self._resolve_agent_code(rm))

    def list_llms(self) -> list[dict[str, Any]]:
        """Read the platform model list inside the worker."""
        rm = self.ensure_manager()
        result = rm.get_client().api.list_agents_v1_llms()

        if isinstance(result, list):
            return result

        if isinstance(result, dict):
            data = result.get("data")

            return data if isinstance(data, list) else []

        return []

    def describe_config_snapshot(self) -> dict[str, Any]:
        """Return the resource manager snapshot inside the worker."""
        return self.ensure_manager().get_config_snapshot()


def _factory_error_to_headless(exc: AgentFactoryError) -> RuntimeFault:
    return RuntimeFault(str(exc.code or "agent.unsupported"), getattr(exc, "reason", None) or exc.message)


def _drain(result: Any) -> None:
    """耗尽 ``agent.execute`` 的结果流（事件已经 event_handler 写出）。"""
    if isinstance(result, str) or result is None:
        return
    for _chunk in result:
        pass
