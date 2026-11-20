# -*- coding: utf-8 -*-
"""worker 内的 SDK 执行体：把 runtime 快照组装成一次单轮运行（\`\`AgentRun\`\`）并执行。**只在子进程里被装载。**

- \`\`AgentRun\`\`：按 \`\`AgentSpec\`\` 与上一轮 \`\`SessionState\`\` 组装 aidev_agent、跑一轮（或一次 prepare / 查询），
  给出合并后的会话状态；
- \`\`SDKExecutor\`\` / \`\`build\`\`：快照的 \`\`agent\`\` 段 → \`\`AgentSpec\`\`（实例化工具、解析 hooks），按 \`\`kind\`\` 分发，
  返回 \`\`{"outcome", "state"}\`\`。worker 端点（\`\`runtime.worker.WorkerContext\`\`）按结构直接充当 \`\`TurnPorts\`\`。

本模块**不**负责跨轮：不持久化、不校验 pending 前置条件、不决定提交或回滚（这些都在 runtime），
也不碰进程级状态（那在 \`\`runtime.worker._worker_entry\`\`）。Agents、writers、hooks 与 savers 都不跨 spawn。

本模块顶部的 SDK 依赖就是本包「SDK 何时进来」的唯一定义点：它被
\`\`aidev_agent_cli.sdk.executor:build\`\` 这个 import target 直接装载，而装载发生在
\`\`runtime.worker._worker_entry\`\` 完成全部保护之后。因此：

* 本模块**不得**再有任何函数内 import——模块级 import 在这里就已经是"晚"的；
* 包根 \`\`sdk.__init__\`\` **不得** import 本模块（那会把 SDK 拉回到父进程与 target 装载之前）；
* 本模块只读 runtime 的 JSON 快照，不 import runtime。
"""

from __future__ import annotations

import asyncio
import importlib
import threading
import uuid
from dataclasses import asdict, replace
from datetime import datetime, timezone
from logging import getLogger
from types import SimpleNamespace
from typing import Any, Protocol

from aidev_agent.enums import AgentBuildType
from aidev_agent.exceptions import AgentFactoryError
from aidev_agent.packages.langchain_core.models.llm_retry import RetryState
from aidev_agent.pydantic_models import ExecuteKwargs

from .checkpoints import CheckpointStore
from .events import STATUS_CANCELLED, STATUS_ERROR, STATUS_INTERRUPT, SessionEventWriter
from .events import event_payload, local_session_writer, platform_session_writer
from .factory import build_agent as build_session_agent
from .factory import ensure_session, validate_message_handler
from .hooks import HeadlessHookBus, build_hook_set
from .interrupts import ITSM_APPROVAL_REASON, RESUMABLE_REASONS, HeadlessInterruptProcessor
from .spec import AgentSpec, LocalPorts, TurnPorts
from .workspace import install_workspace_agent
from ..hooks.contract import HookConfig
from ..utils.runs import RunOutcome, SessionState
from ..utils.errors import (
    AGENT_BUILD_FAILED,
    AGENT_INTERNAL_ERROR,
    RESUME_INTERRUPT_NOT_FOUND,
    RESUME_INTERRUPT_UNSUPPORTED,
    RuntimeFault,
)
from ..resources.loaders.base import ResourceOverrides
from ..resources.manager import HeadlessLedger, LocalResourceManager
from ..resources.sources import uses_platform

logger = getLogger(__name__)

HEADLESS_CHANNEL_TYPE = "headless"


class AgentRun:
    """worker 内的一次 SDK 运行：组装、执行一轮、给出合并后的会话状态。"""

    def __init__(
        self,
        spec: AgentSpec,
        state: SessionState | None = None,
        ports: TurnPorts | None = None,
        *,
        ledger: HeadlessLedger | None = None,
        checkpointer: Any = None,
    ) -> None:
        self.spec = spec
        self._initial_state = state or SessionState()
        self._ports: TurnPorts = ports or LocalPorts()
        self._platform = uses_platform(spec.resources)

        #: 调用方显式给的 saver 优先；否则按 spec 的路径惰性打开、本次运行结束关闭。
        self.checkpointer = checkpointer
        self.checkpoints = CheckpointStore(spec.checkpoint_path) if checkpointer is None else None

        if ledger is None and not self._platform:
            ledger = HeadlessLedger()
        self.ledger: HeadlessLedger | None = ledger
        for record in self._initial_state.records if ledger is not None else ():
            ledger.append(record)

        self._creds: dict[str, str] | None = None
        self._resource_manager: Any | None = None
        self._platform_session_ready = False

        self.writer = SessionEventWriter()
        self.writer.set_session_code(spec.session_code)

        self._state_lock = threading.Lock()
        self._closed = False
        self._cancel_requested = False
        self._current_agent: Any = None
        self._current_turn_id = ""
        self._context_turn = False

        self.hook_bus: HeadlessHookBus | None = None
        if spec.hooks is not None:
            self.hook_bus = HeadlessHookBus(
                config=spec.hooks,
                session_code=spec.session_code,
                channel=SimpleNamespace(request=self._ports.hook),
                writer=self.writer,
                cancel_check=self._cancel_requested_now,
            )
            self.hook_bus.restore(memo=self._initial_state.hook_memo, sequence=self._initial_state.hook_sequence)

        self._turn_count = self._initial_state.turn_count
        self._pending_interrupts: list[dict] = list(self._initial_state.interrupts)

    @property
    def session_code(self) -> str:
        return self.spec.session_code

    @property
    def state(self) -> SessionState:
        """本次运行合并后的会话状态（context 轮不改变状态）；是否提交由 runtime 决定。"""
        if self._context_turn:
            return self._initial_state
        memo, sequence = self.hook_bus.export() if self.hook_bus is not None else ({}, self._initial_state.hook_sequence)
        return SessionState(
            records=self.ledger.records() if self.ledger is not None else [],
            interrupts=list(self._pending_interrupts),
            turn_count=self._turn_count,
            hook_memo=memo,
            hook_sequence=sequence,
        )

    # ------------------------------------------------------------------ 一轮

    def prepare(self) -> None:
        """Validate real SDK assembly; close releases the resulting worker-owned resources."""
        self.ensure_platform_session()
        self._current_agent = self._build(lambda: self._build_agent(None))

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
        self._context_turn = context_mode
        if context_mode and self.hook_bus is not None:
            # context 轮是无状态的一次性调用：不继承跨轮 ask memo。
            self.hook_bus.restore(memo={}, sequence=self._initial_state.hook_sequence)
        return self._drive_turn(
            request_id=request_id,
            input_text=input_text,
            build=lambda hooks: self._build_agent(hooks, messages=list(messages or []) if context_mode else None),
            carried=dict(execute_kwargs or {}),
            sink=sink,
        )

    def resume(
        self,
        *,
        request_id: str,
        resume_payload: Any,
        target: dict | None = None,
        input_text: str = "",
        sink: Any = None,
    ) -> RunOutcome:
        """恢复 runtime 已命中的中断 ``target``（平台来源且无本地 pending 时为 None）；本轮只判定中断类型能否恢复。"""
        item = resume_payload[0] if isinstance(resume_payload, list) else resume_payload

        def build(hooks):
            _check_resumable(target)
            return self._build_agent(hooks, resume_item=item)

        return self._drive_turn(
            request_id=request_id, input_text=input_text, build=build, resume_payload=resume_payload, sink=sink
        )

    def _drive_turn(
        self,
        *,
        request_id: str,
        input_text: str,
        build: Any,
        resume_payload: Any = None,
        carried: dict[str, Any] | None = None,
        sink: Any = None,
    ) -> RunOutcome:
        """Execute and finalize one SDK turn; the parent publishes terminal events only after join."""
        turn_id = self._current_turn_id = uuid.uuid4().hex
        keeps_records = not self._context_turn and self.ledger is not None
        user_message_id = ""
        keep_ask_memo = False
        self.writer.bind(request_id=request_id, thread_id=self.session_code, sink=sink)
        if self.hook_bus is not None:
            self.hook_bus.bind(request_id=request_id, turn_id=turn_id)
        try:
            hooks = self._hook_handlers(turn_id, input_text)
            self.ensure_platform_session()
            agent = self._build(lambda: build(hooks))
            self._run(agent, self._execute_kwargs(turn_id, input_text, resume_payload, carried))
            if keeps_records and not self._has_record("user", turn_id):
                user_message_id = self._append_record("user", input_text, turn_id)

            with self._state_lock:
                cancelled = self._cancel_requested
            if cancelled:
                self.writer.mark_cancelled()
            status = self.writer.finalize()
            if keeps_records:
                answer = self.writer.take_assistant_text()
                if not self._has_record("assistant", turn_id, answer):
                    self._append_record("assistant", answer, turn_id)

            if status == STATUS_INTERRUPT:
                keep_ask_memo = True
                self._pending_interrupts = list(self.writer.interrupts)
                outcome = RunOutcome(
                    "interrupted", session_code=self.session_code, turn_id=turn_id,
                    interrupts=list(self._pending_interrupts),
                )
            elif status == STATUS_ERROR:
                self._pending_interrupts = []
                outcome = RunOutcome(
                    "failed", session_code=self.session_code, turn_id=turn_id, error_code=AGENT_INTERNAL_ERROR,
                    error_message=self.writer.error_message or "本轮执行失败：执行链返回 RUN_ERROR",
                )
            else:
                self._pending_interrupts = []
                outcome = RunOutcome(
                    "cancelled" if status == STATUS_CANCELLED else "finished",
                    session_code=self.session_code, turn_id=turn_id,
                )
        except Exception as exc:  # pylint: disable=broad-except
            if user_message_id:
                self.ledger.drop(user_message_id)
            code, message, event_message = _failure(exc)
            if not isinstance(exc, (RuntimeFault, AgentFactoryError)):
                logger.exception("headless turn failed: session_code=%s", self.session_code)
            self.writer.emit_error(event_message)
            outcome = RunOutcome(
                "failed", session_code=self.session_code, turn_id=turn_id, error_code=code, error_message=message
            )
        finally:
            self._turn_count += 1
            if self.hook_bus is not None and not keep_ask_memo:
                self.hook_bus.reset_turn()
        # 运行结束后宿主仍判定取消（取消晚于 SDK 收尾）时，本轮以 cancelled 落定。
        if outcome.status != "cancelled" and self._ports.cancelled():
            outcome = replace(outcome, status="cancelled")
        return outcome

    def close(self) -> None:
        """Release worker-local SDK resources and the checkpoint connection opened by this run."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            agent, self._current_agent = self._current_agent, None
        try:
            if agent is not None:
                agent.stop()
        finally:
            if self.ledger is not None:
                self.ledger.clear()
            self.checkpointer = None
            if self.checkpoints is not None:
                self.checkpoints.close()

    # ------------------------------------------------------------------ 组装

    def _build(self, builder):
        """组装 agent：经 ``ports.building`` 上报构建阶段，并把 SDK 组装异常归一为 ``RuntimeFault``。"""
        return self._ports.building(lambda: _build_guarded(builder))

    def _build_agent(self, hook_handlers: Any, *, messages: list[dict] | None = None, resume_item: dict | None = None):
        """本轮 agent：会话轮 SESSION；context 轮 DIRECT + 给定消息；resume 轮 SESSION + 恢复项。"""
        params: dict[str, Any] = {
            "build_type": AgentBuildType.DIRECT if messages is not None else AgentBuildType.SESSION,
            "checkpointer": self.checkpointer if self.checkpointer is not None else self.checkpoints.ensure_sync(),
            "event_handler": self._event_handler(),
            "channel_type": HEADLESS_CHANNEL_TYPE,
            "hook_handlers": hook_handlers,
            "interrupt_processor": HeadlessInterruptProcessor(resume_item=resume_item)
            if resume_item is not None
            else HeadlessInterruptProcessor(),
        }
        if resume_item is not None:
            params["resume"] = resume_item
        else:
            params["session_context_data"] = messages
        return self.build_agent(**params)

    def build_agent(self, **params: Any) -> Any:
        """Assemble an SDK agent with worker-owned resources, saver, tools and real session writer."""
        rm = self.ensure_manager()
        params.setdefault("checkpointer", self.checkpointer)
        spec = self.spec
        return build_session_agent(
            rm,
            spec.session_code,
            self._resolve_agent_code(rm),
            spec.username or None,
            temperature=spec.temperature,
            max_tokens=spec.max_tokens,
            auth_headers=spec.auth_headers,
            base_url=spec.base_url or None,
            injected_tools=list(spec.tools) if spec.tools else None,
            **params,
        )

    def _event_handler(self) -> Any:
        """本轮的 SDK 事件处理器：无平台时就是会话 writer；平台来源时是「平台回写 + 转发」组合 writer。"""
        if not self._platform:
            return local_session_writer(
                session_code=self.session_code,
                username=self.spec.username,
                ledger=self.ledger,
                forward=self.writer,
                turn_id=self._current_turn_id,
            )
        rm = self.ensure_manager()
        return platform_session_writer(
            session_code=self.session_code,
            client=rm.get_client(),
            username=self.spec.username or getattr(rm, "username", ""),
            forward=self.writer,
        )

    def _hook_handlers(self, turn_id: str, input_text: str) -> Any:
        """本轮注入 SDK 的 ``HookSet``；未配置 hooks 或点位全关时为 None（零回归）。"""
        if self.hook_bus is None:
            return None
        return build_hook_set(self.hook_bus, turn_id=turn_id, input_text=input_text or "", first_turn=self._turn_count == 0)

    def _execute_kwargs(self, turn_id: str, input_text: str, resume_payload: Any, carried: dict | None) -> ExecuteKwargs:
        """本轮执行参数：三个协议同一组基础键；平台来源追加持久化输入；调用方透传的覆盖在最后。"""
        values: dict[str, Any] = {
            "stream": True,
            "session_code": self.session_code,
            "thread_id": self.session_code,
            "turn_id": turn_id,
            "input": input_text or "",
            "resume": resume_payload,
        }
        if self._platform:
            values.update({"stream_mode": "start", "persist_input": True})
        values.update({key: value for key, value in (carried or {}).items() if key != "resume"})
        return ExecuteKwargs.model_validate(values)

    def ensure_platform_session(self) -> None:
        """带平台来源时，让平台知晓本会话（本次运行内幂等；无平台来源时什么也不做）。

        SESSION 模式下 SDK 会向平台拉取会话历史，未知的 code 会 404。
        """
        rm = self.ensure_manager()
        if self._platform_session_ready or not getattr(rm, "uses_platform", True):
            return
        ensure_session(rm, self.session_code)
        self._platform_session_ready = True

    def ensure_manager(self) -> LocalResourceManager:
        """Build worker-owned resources from the accepted identity and explicit overrides."""
        if self._resource_manager is None:
            spec = self.spec
            self._creds = dict(spec.credentials) if self._platform else {}
            agent_code = self._resolve_agent_code(None)
            self._resource_manager = LocalResourceManager.build(
                self._creds,
                spec.username,
                access_token=spec.access_token,
                overrides=ResourceOverrides(model=spec.model, agent_code=agent_code),
                sources=tuple(spec.resources),
                agent_code=agent_code,
                model=spec.model,
                ledger=self.ledger,
            )
        return self._resource_manager

    def _resolve_agent_code(self, rm: LocalResourceManager | None) -> str:
        """Use the accepted agent selection, then the worker's platform default."""
        return (
            self.spec.agent_code
            or (self._creds or {}).get("BKPAAS_APP_CODE", "")
            or (rm.get_agent_code() if rm is not None else "")
        )

    # ------------------------------------------------------------------ 执行与取消

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

        if self.spec.rate_limit_retries <= 0:
            _drain(agent.execute(execute_kwargs))
            return

        def report_retry(event: dict) -> None:
            if self._cancel_requested_now():
                retry_state.cancel()
            self.writer.emit(event)

        retry_state = RetryState(
            max_retries=self.spec.rate_limit_retries,
            wait_budget=self.spec.rate_limit_wait_budget,
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

    def request_stop(self) -> None:
        """Stop the worker-local SDK agent cooperatively; parent ProcessWorker enforces kill/join."""
        with self._state_lock:
            if self._cancel_requested:
                return
            self._cancel_requested = True
            agent = self._current_agent
        self.writer.mark_cancelled()
        if agent is not None:
            try:
                agent.stop()
            except Exception:  # pylint: disable=broad-except
                logger.warning("headless cancel: agent.stop failed: session_code=%s", self.session_code, exc_info=True)

    def _cancel_requested_now(self) -> bool:
        """取消的唯一判定：已请求停止，或宿主经 ``ports.cancelled`` 要求取消（首次命中即停止 agent）。"""
        with self._state_lock:
            if self._cancel_requested:
                return True
        if self._ports.cancelled():
            self.request_stop()
            return True
        return False

    # ------------------------------------------------------------------ 账本

    def _has_record(self, role: str, turn_id: str, content: str | None = None) -> bool:
        return any(
            record.get("role") == role
            and record.get("turn_id") == turn_id
            and (content is None or record.get("content") == content)
            for record in self.ledger.records()
        )

    def _append_record(self, role: str, content: str, turn_id: str) -> str:
        """写一条本轮记录（空内容不写）；返回 message_id，供失败回滚。"""
        if not content:
            return ""
        message_id = uuid.uuid4().hex
        self.ledger.append(
            {
                "role": role,
                "content": content,
                "message_id": message_id,
                "turn_id": turn_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "status": "success",
                "property": {"extra": {}},
            }
        )
        return message_id

    # ------------------------------------------------------------------ 查询

    def describe_agent_config(self) -> dict[str, Any]:
        """Return the manager projection inside the worker."""
        rm = self.ensure_manager()
        return rm.describe_agent_config(self._resolve_agent_code(rm))

    def list_llms(self) -> list[dict[str, Any]]:
        """Read the platform model list inside the worker."""
        result = self.ensure_manager().get_client().api.list_agents_v1_llms()
        data = result.get("data") if isinstance(result, dict) else result
        return data if isinstance(data, list) else []

    def describe_config_snapshot(self) -> dict[str, Any]:
        """Return the resource manager snapshot inside the worker."""
        return self.ensure_manager().get_config_snapshot()


def _build_guarded(builder):
    """Normalize real SDK assembly failures; parent ProcessWorker bounds build duration."""
    try:
        return builder()
    except (RuntimeFault, AgentFactoryError):
        raise
    except Exception as exc:  # pylint: disable=broad-except
        raise RuntimeFault(AGENT_BUILD_FAILED, f"{type(exc).__name__}: {exc}") from exc


def _failure(exc: Exception) -> tuple[str, str, str]:
    """失败轮的 (error_code, error_message, RUN_ERROR 消息)。"""
    if isinstance(exc, RuntimeFault):
        return exc.code, exc.message, exc.message
    if isinstance(exc, AgentFactoryError):
        reason = getattr(exc, "reason", None)
        return str(exc.code or "agent.unsupported"), reason or exc.message, f"{exc.code}: {reason}"
    text = f"{type(exc).__name__}: {exc}"
    return AGENT_INTERNAL_ERROR, text, text


def _check_resumable(target: dict | None) -> None:
    """本轮能否恢复该中断类型（ITSM 审批不接线；只接 hook ask 与向用户提问）。"""
    if target is None:
        return
    reason = target.get("reason")
    if reason == ITSM_APPROVAL_REASON:
        raise RuntimeFault(
            RESUME_INTERRUPT_UNSUPPORTED,
            "headless 本期不接线 ITSM 权限审批中断的恢复，请由平台侧完成审批",
            {"reason": reason},
        )
    if reason not in RESUMABLE_REASONS:
        raise RuntimeFault(RESUME_INTERRUPT_NOT_FOUND, "该中断类型不在 headless 可恢复范围内", {"reason": reason})


def _drain(result: Any) -> None:
    """耗尽 ``agent.execute`` 的结果流（事件已经 event_handler 写出）。"""
    if isinstance(result, str) or result is None:
        return
    for _chunk in result:
        pass


# ============================== 执行体（worker import target） ==============================

QUERY_KINDS = ("describe_agent_config", "list_llms", "describe_config_snapshot")


class WorkerPorts(TurnPorts, Protocol):
    """worker 端点：单轮端口 + 本轮事件出口。"""

    def emit(self, event: Any) -> None: ...


def _tool(target: str) -> Any:
    module, separator, name = target.partition(":")
    if not separator or not module or not name or "<locals>" in name:
        raise ValueError("tool factories must be import targets module:name")
    factory = importlib.import_module(module)
    for component in name.split("."):
        factory = getattr(factory, component)
    return factory()


def _spec(agent: dict[str, Any]) -> AgentSpec:
    fields = dict(agent)
    hooks = fields.pop("hooks")
    tools = [_tool(target) for target in fields.pop("tool_factories")]
    return AgentSpec(
        **fields,
        hooks=HookConfig.parse(hooks) if hooks is not None else None,
        tools=tools,
    )


class SDKExecutor:
    def __init__(self, snapshot: dict[str, Any], ports: WorkerPorts) -> None:
        validate_message_handler()
        install_workspace_agent()
        self.snapshot = snapshot
        self.run = AgentRun(_spec(snapshot["agent"]), SessionState.from_dict(snapshot["state"]), ports)

    def execute(self, ports: WorkerPorts) -> dict[str, Any]:
        run = self.run
        request = self.snapshot
        kind = request["kind"]
        typed_input = request["input"]

        def deliver(event: Any) -> None:
            ports.emit(event_payload(event))

        if kind in QUERY_KINDS:
            return getattr(run, kind)()
        if kind == "prepare":
            run.prepare()
            return {"status": "finished", "session_code": run.session_code}
        if kind == "resume":
            outcome = run.resume(
                request_id=request["correlation_id"],
                input_text=typed_input["text"],
                resume_payload=typed_input["payload"],
                target=request["resume_target"],
                sink=deliver,
            )
        else:
            outcome = run.turn(
                request_id=request["correlation_id"],
                input_text=typed_input["text"],
                messages=typed_input.get("messages"),
                context_mode=typed_input["kind"] == "context",
                execute_kwargs=request["execute_options"],
                sink=deliver,
            )
        return {"outcome": asdict(outcome), "state": asdict(run.state)}

    def close(self) -> None:
        self.run.close()


def build(snapshot: dict[str, Any], ports: WorkerPorts) -> SDKExecutor:
    return SDKExecutor(snapshot, ports)


__all__ = ["AgentRun", "SDKExecutor", "build"]
