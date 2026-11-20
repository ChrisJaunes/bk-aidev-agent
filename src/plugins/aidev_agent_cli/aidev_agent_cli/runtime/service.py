"""Sole application entrance: atomic admission snapshots and shared closing tasks."""

from __future__ import annotations

import asyncio
import copy
import os
from dataclasses import dataclass, replace
from logging import getLogger

from .auth import AuthManager, ProcessConfig
from .contracts import EventConsumer, LaunchOptions, ResumeRequest, RunHandle, RunRequest, SessionOptions
from .errors import RuntimeFault
from .inputs import ChatInput, ContextInput, ResumeInterrupt
from .models import HookHandler, IdentitySnapshot, SessionSnapshot, SettingsSnapshot, TurnSnapshot
from .runs import CANCEL_GRACE_SECONDS, CancelOutcome, FIFOScheduler
from .session import SessionInfo, SessionRegistry
from ..infrastructure.settings import TuiConfig, resolve_acp_access_token
from ..resources.local_config import LocalDeclarations
from ..resources.sources import uses_platform

logger = getLogger(__name__)


@dataclass
class CloseOutcome:
    session_code: str
    closed: bool
    already_closed: bool


class RuntimeService:
    def __init__(
        self, *, config: ProcessConfig | None = None, auth: AuthManager | None = None, busy_mode: str = "reject"
    ) -> None:
        self.state = "running"
        self._shutdown_task = None
        self._session_closes = {}
        self._control_lock = asyncio.Lock()
        self._revision = 0
        self._workspace_root = os.getcwd()
        self._config = config or ProcessConfig()
        self._auth = auth or AuthManager()
        self._sessions = SessionRegistry(self._config.defaults)
        self._scheduler = FIFOScheduler(self._sessions, busy_mode=busy_mode, on_reap_failure=self._worker_reap_failed)

    def _worker_reap_failed(self):
        self.state = "closing"

    @property
    def defaults(self):
        return self._config.defaults

    async def default_agent_code(self):
        return await asyncio.to_thread(self._config.default_agent_code)

    async def has_token(self):
        return self._auth.has_token()

    @property
    def closing(self):
        return self.state != "running"

    def _ensure_running(self):
        if self.closing:
            raise RuntimeFault("runtime.closing", "运行时正在关闭")

    def _require(self, code):
        self._ensure_running()
        info = self._sessions.info(code)
        if info is None or info.closed:
            raise RuntimeFault("session.not_found", "未知或已关闭会话")
        session = self._sessions.get(code)
        session.ensure_open()
        return session

    async def new_session(self, options: SessionOptions | None = None) -> SessionInfo:
        self._ensure_running()
        async with self._control_lock:
            self._ensure_running()
            if options is not None:
                if self._sessions.info(options.session_code) is not None:
                    raise RuntimeFault("session.exists", "会话已登记")
                if not isinstance(options.session_code, str) or not options.session_code:
                    raise RuntimeFault("session.invalid", "会话标识必须是非空字符串")
                if not all(
                    isinstance(target, str) and ":" in target and "<locals>" not in target
                    for target in options.tool_factories
                ):
                    raise RuntimeFault("tools.invalid", "工具必须是可序列化 module:factory 导入目标")
                options = replace(
                    options,
                    auth_headers=copy.deepcopy(options.auth_headers),
                    resources=tuple(options.resources),
                    tool_factories=tuple(options.tool_factories),
                )
            return await asyncio.to_thread(self._sessions.new_session, options)

    async def session_info(self, code):
        info = self._sessions.info(code)
        return replace(info) if info else None

    async def list_sessions(self):
        return self._sessions.list()

    async def latest_session_code(self):
        session = self._sessions.latest()
        return session.session_code if session is not None and not session.closed else None

    def run_handles(self):
        return self._scheduler.handles()

    def has_live_runs(self, code):
        return self._scheduler.busy(code)

    def _snapshot(self, kind, session, request, hooks):
        options = session.options
        value = request.input
        identity = self._auth.snapshot()
        return TurnSnapshot(
            session=SessionSnapshot(
                session_code=session.session_code,
                agent_code=options.agent_code or self._config.default_agent_code(),
                resources=options.resources,
                model=options.model,
                identity=IdentitySnapshot(
                    options.username or identity.username, identity.access_token, identity.revision
                ),
                settings=SettingsSnapshot(
                    self._workspace_root, {**os.environ, **self._config.env}, revision=self._revision + session.revision
                ),
                base_url=options.base_url,
                auth_headers=copy.deepcopy(options.auth_headers),
                temperature=options.temperature,
                max_tokens=options.max_tokens,
                tool_factories=options.tool_factories,
            ),
            kind=kind,
            correlation_id=request.correlation_id,
            run_id="",
            input=copy.deepcopy(value),
            execute_options=copy.deepcopy(request.execute_options) if isinstance(request, RunRequest) else {},
            hooks={"timeout_ms": hooks.timeout_ms, "points": sorted(hooks.points())} if hooks else None,
        )

    async def _admit(self, kind, request, sink=None, *, hooks=None, hook_handler=None):
        self._ensure_running()
        async with self._control_lock:
            session = self._require(request.session_code)
            if kind == "start":
                if not isinstance(request.input, (ChatInput, ContextInput)):
                    raise RuntimeFault("input.invalid", "start 需要 ChatInput 或 ContextInput")
                if not isinstance(request.execute_options, dict):
                    raise RuntimeFault("input.invalid", "execute_options 必须是对象")
                if set(request.execute_options) & {"input", "session_code", "thread_id", "turn_id"}:
                    raise RuntimeFault("input.invalid", "执行选项不能覆盖运行身份或 typed input")
                if "resume" in request.execute_options:
                    raise RuntimeFault("input.invalid", "中断恢复必须调用 resume")
                if session.pending and isinstance(request.input, ChatInput):
                    raise RuntimeFault("session.pending_interrupt", "请先恢复待处理中断")
            elif kind == "resume" and not isinstance(request.input, ResumeInterrupt):
                raise RuntimeFault("resume_payload_unsupported", "resume 需要 ResumeInterrupt")
            if hooks is not None and hook_handler is None:
                raise RuntimeFault("hook.value_invalid", "启用 hook 必须提供本轮 hook_handler")
            snapshot = self._snapshot(kind, session, request, hooks)
            return await self._scheduler.submit(request, sink=sink, hook_handler=hook_handler, snapshot=snapshot)

    async def start(
        self,
        request: RunRequest,
        sink: EventConsumer | None = None,
        *,
        hooks=None,
        hook_handler: HookHandler | None = None,
    ) -> RunHandle:
        return await self._admit("start", request, sink, hooks=hooks, hook_handler=hook_handler)

    async def resume(
        self,
        request: ResumeRequest,
        sink: EventConsumer | None = None,
        *,
        hooks=None,
        hook_handler: HookHandler | None = None,
    ) -> RunHandle:
        return await self._admit("resume", request, sink, hooks=hooks, hook_handler=hook_handler)

    async def prepare(self, session_code):
        handle = await self._admit("prepare", RunRequest(session_code))
        return await handle.result()

    async def _query(self, code, kind):
        handle = await self._admit(kind, RunRequest(code))
        outcome = await handle.result()
        if not outcome.ok:
            raise RuntimeFault(outcome.error_code, outcome.error_message)
        return outcome.data

    async def describe_agent_config(self, session_code):
        return await self._query(session_code, "describe_agent_config")

    async def list_llms(self, session_code):
        return await self._query(session_code, "list_llms")

    async def describe_config_snapshot(self, session_code):
        return await self._query(session_code, "describe_config_snapshot")

    async def login(self, token):
        self._ensure_running()
        async with self._control_lock:
            self._ensure_running()
            await asyncio.to_thread(self._auth.login, token)

    async def logout(self):
        self._ensure_running()
        async with self._control_lock:
            self._ensure_running()
            await asyncio.to_thread(self._auth.logout)

    async def set_local_declaration(self, code, kind, value):
        self._ensure_running()
        async with self._control_lock:
            session = self._require(code)
            if self._scheduler.busy(code):
                raise RuntimeFault("session.busy", "运行期间不能修改配置")
            if kind not in ("agents", "models", "mcps", "knowledge") or not isinstance(value, str) or not value:
                raise RuntimeFault("config.invalid", "无效配置项")
            if kind == "agents":
                session.options = replace(session.options, agent_code=value)
            else:
                agent_code = session.options.agent_code or self._config.default_agent_code()
                await asyncio.to_thread(LocalDeclarations().write, kind, value, code=agent_code)
                if kind == "models":
                    session.options = replace(session.options, model=value)
            session.revision += 1
            self._revision += 1

    async def cancel(self, session_code, run_id=None):
        return await self._scheduler.cancel(session_code, run_id)

    async def close(self, session_code):
        task = self._session_closes.get(session_code)
        if task is not None:
            await asyncio.shield(task)
            return CloseOutcome(session_code, False, True)
        info = self._sessions.info(session_code)
        if info is None or info.closed:
            return CloseOutcome(session_code, False, True)
        self._sessions.get(session_code).close()
        task = asyncio.create_task(self._close_session(session_code))
        self._session_closes[session_code] = task
        return await asyncio.shield(task)

    async def _close_session(self, code):
        await self._scheduler.close_session(code)
        self._sessions.get(code).close()
        return CloseOutcome(code, True, False)

    async def close_all(self, reason="shutdown", *, grace=None):
        if grace is not None and (not isinstance(grace, (int, float)) or grace < 0):
            raise RuntimeFault("shutdown.invalid", "grace 必须是非负秒数")
        if self._shutdown_task is None:
            self.state = "closing"
            self._scheduler.begin_close()
            self._shutdown_task = asyncio.create_task(self._shutdown(reason, grace))
        await asyncio.shield(self._shutdown_task)

    async def _shutdown(self, reason, grace):
        async with self._control_lock:
            await self._scheduler.close_all(reason, grace=grace)
            await asyncio.gather(*(self.close(code) for code in self._sessions.codes()))
            self._sessions.close_all()
            self.state = "closed"
            logger.info("runtime closed")


#: 协议 → busy 语义（入口唯一的一张表）。
#:
#: 同会话第二轮的处置：acp-dsh 排队串行（dsh 的 followup 本就串行）；agui / acp 立即拒绝
#: （它们的请求要求立即应答）。资源默认值不在这张表里：那是入口 ``resolve_resources`` 的事。
PROTOCOL_BUSY_MODES: dict[str, str] = {
    "agui": "reject",
    "acp": "reject",
    "acp-dsh": "wait",
}


def build_runtime(
    protocol: str,
    *,
    model: str = "",
    session: str = "",
    resources: tuple[str, ...],
    env: dict[str, str] | None = None,
    auth: AuthManager | None = None,
) -> "RuntimeService":
    """**唯一**的 ``RuntimeService`` 组装点：入口、测试与嵌入式调用方都经它。

    进程级配置（命令行默认值 / env）、身份（acp 带平台来源时解析启动 token，只在内存生效）、
    checkpoint 存储与 busy 语义都在这里决定；adapter 只接收组装好的 service。

    ``resources`` 是入口解析好的资源序列（含省略 ``--resources`` 时的默认预设）：这里不为
    它兜底——会话拿到什么就是什么。
    阻塞且会导入 SDK：调用方须在启动钩子之后调用。
    """
    busy_mode = PROTOCOL_BUSY_MODES[protocol]
    defaults = LaunchOptions(model=model, session=session, resources=tuple(resources))

    if auth is None and protocol == "acp" and uses_platform(resources):
        # 缺 token 在任何连接绑定之前失败（stderr + 非零退出），绝不写进协议通道。
        auth = AuthManager(access_token=resolve_acp_access_token())

    return RuntimeService(
        config=ProcessConfig(env=dict(env) if env is not None else TuiConfig.read().env, defaults=defaults),
        auth=auth,
        busy_mode=busy_mode,
    )


__all__ = [
    "PROTOCOL_BUSY_MODES",
    "build_runtime",
    "CANCEL_GRACE_SECONDS",
    "CancelOutcome",
    "CloseOutcome",
    "RuntimeService",
    "SessionInfo",
]