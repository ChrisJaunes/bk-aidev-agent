# -*- coding: utf-8 -*-
"""ACP-DSH protocol: standard ACP plus explicit hook/resume extensions.

Each run receives independent event and hook bridges through RuntimeService.
``HookConfig`` 来自 ``runtime.hook_contract``：超时上限与点位集合都是纯值（点位名是线上
契约字符串），父进程解析它不需要 SDK；``HookReply`` 等纯值对象在 ``runtime.models``。
"""

from __future__ import annotations

import asyncio
from logging import getLogger
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, run_agent
from acp.schema import AgentCapabilities, CloseSessionResponse, InitializeResponse, NewSessionResponse, PromptResponse

from .acp import (
    EXT_HOOK,
    EXT_HOOK_DEGRADED,
    EXT_RESUME,
    META_KEY,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    AguiEventInterpreter,
    HookTransport,
    _serve_until_signal,
    _StdioTransport,
    _text_of_content_blocks,
    notify_hook_degraded,
    outcome_meta,
    session_meta,
    session_options_from_meta,
    translate_runtime_fault,
)
from ..infrastructure.errors import (
    AGENT_BUILD_FAILED,
    AGENT_INTERNAL_ERROR,
    PROTOCOL_INVALID_PARAMS,
    SESSION_NOT_FOUND,
    HeadlessError,
)
from ..infrastructure.stdio import protocol_out
from ..runtime.contracts import ResumeRequest, RunOutcome, RunRequest, SessionOptions, new_session_code
from ..runtime.errors import RuntimeFault
from ..runtime.hook_contract import HookConfig
from ..runtime.inputs import ChatInput, ContextInput, ResumeInterrupt
from ..runtime.service import RuntimeService

logger = getLogger(__name__)

PROTOCOL_NAME = "acp-dsh"
PROTOCOL_REVISION = 1


def _rpc_error(code: str, message: str, detail: dict | None = None) -> RequestError:
    """稳定错误码 → JSON-RPC error；``data.code`` 与 headless 错误码同一张表，宿主据此分流。"""
    rpc_code = -32602 if code == PROTOCOL_INVALID_PARAMS else -32603
    data: dict[str, Any] = {"code": code, "message": message}
    if detail:
        data["detail"] = detail
    return RequestError(rpc_code, message, data)


def _from_headless(exc: HeadlessError) -> RequestError:
    return _rpc_error(exc.code, exc.message, exc.detail)


class _SessionBridge:
    """Per-run AG-UI to ACP event interpreter; no cross-turn execution state."""

    def __init__(self, adapter: "AcpDshAdapter", session_id: str) -> None:
        self._adapter = adapter
        self._session_id = session_id
        self.interpreter = AguiEventInterpreter()

    async def send(self, payload: dict[str, Any]) -> None:
        await self._send_event(payload)

    async def _send_event(self, payload: dict[str, Any]) -> None:
        conn = self._adapter.conn
        if await notify_hook_degraded(conn, self._session_id, payload):
            return
        for message in self.interpreter.interpret(payload):
            if conn is not None:
                await conn.session_update(self._session_id, message)


class AcpDshAdapter:
    """ACP ``Agent`` 实现，后端为统一会话（spawned SDK worker），运行状态全在 ``RuntimeService``。"""

    def __init__(self, service: RuntimeService, *, tool_factories: tuple[str, ...] = ()) -> None:
        #: 进程内注入工具（嵌入式接缝：入口脚本可传，不属于线上协议）。
        self._tool_factories = tuple(tool_factories)
        self._hooks = {}
        #: 进程级运行时（入口组装；同会话排队串行等协议默认值见 build_runtime）。
        self.service = service
        self.conn: Any = None

    def on_connect(self, conn: Any) -> None:
        self.conn = conn

    async def initialize(
        self, protocol_version: int, client_capabilities: Any = None, client_info: Any = None, **kwargs: Any
    ) -> Any:
        capabilities = AgentCapabilities.model_validate({"sessionCapabilities": {"close": {}}})
        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=capabilities,
            field_meta={
                META_KEY: {
                    "protocol": PROTOCOL_NAME,
                    "revision": PROTOCOL_REVISION,
                    "agentMethods": [f"_{EXT_RESUME}"],
                    "clientMethods": [f"_{EXT_HOOK}", f"_{EXT_HOOK_DEGRADED}"],
                }
            },
        )

    # ------------------------------------------------------------ 会话

    @translate_runtime_fault
    async def new_session(self, cwd: str, additional_directories: Any = None, mcp_servers: Any = None, **kwargs: Any):
        try:
            meta = session_meta(kwargs)
        except (HeadlessError, RuntimeFault) as exc:
            raise _from_headless(exc) from exc
        session_code = meta.get("sessionCode") or self.service.defaults.session or new_session_code()
        if await self.service.session_info(session_code) is not None:
            raise _rpc_error(PROTOCOL_INVALID_PARAMS, f"会话已存在：{session_code}", {"fields": ["sessionCode"]})
        if mcp_servers:
            logger.info("acp-dsh: client mcp_servers ignored")
        try:
            options = self._session_options(session_code, meta, cwd)
            if not options.agent_code and not await self.service.default_agent_code():
                raise HeadlessError(PROTOCOL_INVALID_PARAMS, "缺少主 agent 配置", {"fields": ["agent"]})
            # create + prepare 会导入 / 装配 SDK：放线程里，连接循环保持可用。
            await self._create(options)
        except (HeadlessError, RuntimeFault) as exc:
            self._hooks.pop(session_code, None)
            raise _from_headless(exc) from exc
        return NewSessionResponse(session_id=session_code)

    def _session_options(self, session_code: str, meta: dict[str, Any], cwd: Any = None) -> SessionOptions:
        logger.info(f"new_session: cwd={cwd!r} session_id={session_code!r}")
        options = session_options_from_meta(
            meta,
            session_code=session_code,
            tool_factories=self._tool_factories,
        )
        raw_hooks = meta.get("hooks")
        self._hooks[session_code] = HookConfig.parse(raw_hooks) if raw_hooks is not None else None
        return options

    async def _create(self, options: SessionOptions) -> None:
        try:
            await self.service.new_session(options)
            outcome = await self.service.prepare(options.session_code)
            if not outcome.ok:
                raise HeadlessError(AGENT_BUILD_FAILED, outcome.error_message or "装配失败")
        except Exception:
            await self.service.close(options.session_code)
            raise

    async def _require(self, session_id):
        info = await self.service.session_info(session_id)
        if info is None or info.closed:
            raise _rpc_error(SESSION_NOT_FOUND, "未知或已关闭会话")
        return _SessionBridge(self, session_id)

    # ------------------------------------------------------------ 轮次

    @translate_runtime_fault
    async def prompt(self, session_id: str, prompt: Any, **kwargs: Any):
        bridge = await self._require(session_id)
        meta = kwargs.get(META_KEY) if isinstance(kwargs.get(META_KEY), dict) else {}
        messages = meta.get("messages")
        request = RunRequest(
            session_code=session_id,
            input=ContextInput(messages, _text_of_content_blocks(prompt))
            if isinstance(messages, list)
            else ChatInput(_text_of_content_blocks(prompt)),
            correlation_id=new_session_code(),
        )
        handle = await self.service.start(
            request, bridge, hooks=self._hooks.get(session_id), hook_handler=HookTransport(self.conn, session_id)
        )
        return self._response(await handle.result())

    @translate_runtime_fault
    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method != EXT_RESUME:
            raise RequestError.method_not_found(f"_{method}")
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise _rpc_error(PROTOCOL_INVALID_PARAMS, "缺少必填字段：sessionId", {"fields": ["sessionId"]})
        bridge = await self._require(session_id)
        payload = params.get("resume")
        text = params.get("input") or ""
        if payload is None or isinstance(payload, dict) and "interruptId" not in payload:
            request = RunRequest(
                session_id, ChatInput(text or (payload or {}).get("input", "")), correlation_id=new_session_code()
            )
            handle = await self.service.start(
                request, bridge, hooks=self._hooks.get(session_id), hook_handler=HookTransport(self.conn, session_id)
            )
        else:
            request = ResumeRequest(session_id, ResumeInterrupt(payload, text), correlation_id=new_session_code())
            handle = await self.service.resume(
                request, bridge, hooks=self._hooks.get(session_id), hook_handler=HookTransport(self.conn, session_id)
            )
        response = self._response(await handle.result())
        return response.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.info(f"acp-dsh: ignoring extension notification _{method}")

    @staticmethod
    def _response(outcome: RunOutcome) -> PromptResponse:
        """``RunOutcome`` → prompt 响应；失败 / 被拒一律 JSON-RPC error（不得说成 end_turn）。"""
        if outcome.status in ("failed", "rejected"):
            raise _rpc_error(outcome.error_code or AGENT_INTERNAL_ERROR, outcome.error_message or "执行失败")
        meta = outcome_meta(outcome)
        stop = STOP_REASON_CANCELLED if outcome.status == "cancelled" else STOP_REASON_END_TURN
        return PromptResponse(stop_reason=stop, field_meta={META_KEY: meta})

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        if await self.service.session_info(session_id) is not None:
            await self.service.cancel(session_id)

    async def close_session(self, session_id: str, **kwargs: Any):
        """幂等：关闭未知 / 已关会话也是成功。"""
        if await self.service.session_info(session_id) is not None:
            if self.service.has_live_runs(session_id):
                await self.service.cancel(session_id)
            await self.service.close(session_id)
        self._hooks.pop(session_id, None)
        return CloseSessionResponse()


async def run_acp_dsh_server(
    service: RuntimeService, *, tool_factories: tuple[str, ...] = (), handle_signals: bool = False
) -> int:
    """跑 acp-dsh 服务端直到 EOF / 信号。``service`` **必须**由调用方组装好。"""
    adapter = AcpDshAdapter(service, tool_factories=tool_factories)
    try:
        serving = run_agent(adapter, _StdioTransport(protocol_out()))
        if handle_signals:
            await _serve_until_signal(serving)
        else:
            await serving
    finally:
        await adapter.service.close_all("acp-dsh eof")
    return 0


def run_acp_dsh(service: RuntimeService, *, tool_factories: tuple[str, ...] = ()) -> int:
    """acp-dsh 入口：``service`` 由调用方经 ``build_runtime("acp-dsh", ...)`` 组装；
    仓外入口脚本可额外注入进程内工具（``tools``）。"""
    return asyncio.run(run_acp_dsh_server(service, tool_factories=tool_factories, handle_signals=True))


__all__ = ["AcpDshAdapter", "PROTOCOL_NAME", "run_acp_dsh", "run_acp_dsh_server"]