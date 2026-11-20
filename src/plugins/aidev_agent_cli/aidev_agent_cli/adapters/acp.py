# -*- coding: utf-8 -*-
"""ACP 服务端：将 RuntimeService 的 AG-UI 事件转换为 ACP 响应与通知。

会话与运行生命周期由 runtime 管理；每轮独享 sink 和解释器，避免串会话。
SDK 并发处理请求，因此 prompt 等待终态时仍可接收 cancel。失败抛 AcpError，
由 SDK 返回 JSON-RPC error；中断以文本降级，并通过扩展元数据提供恢复信息。

本模块顶层导入 SDK，入口须先安装 stdout guard 并发布环境。fd 1 仅用于
JSON-RPC，诊断走 logger。服务退出时始终 await service.close_all。

两处 SDK 边界的区分：``HookReply`` 是**纯值对象**（父进程侧构造回复），取
``runtime.models``；``HookConfig`` 解析 ``hooks`` 配置（超时上限 + 点位集合），同为纯值，
取 ``runtime.hook_contract``——点位名是线上契约字符串，父进程解析它不需要 SDK。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Awaitable
from functools import wraps
from logging import getLogger
from typing import Any

from acp import PROTOCOL_VERSION, RequestError, run_agent
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    InitializeResponse,
    NewSessionResponse,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
)

from ..infrastructure.errors import PROTOCOL_INVALID_PARAMS, HeadlessError
from ..infrastructure.process import async_shutdown_event, use_daemon_default_executor
from ..infrastructure.stdio import protocol_out
from ..infrastructure.streams import LineReader, LineWriter, until_stopped
from ..runtime.contracts import ResumeRequest, RunOutcome, RunRequest, SessionOptions, new_session_code
from ..runtime.errors import RuntimeFault
from ..runtime.hook_contract import HookConfig
from ..runtime.inputs import ChatInput, ResumeInterrupt
from ..runtime.models import HookReply
from ..runtime.service import RuntimeService

# EOF 后已接收请求全部响应时，额外等待输出静默的阈值（秒）。
EOF_QUIET_SECONDS = 0.2

# EOF 之后的排空硬上限（秒）：某个 handler 卡住时也必须能退出。
EOF_DRAIN_SECONDS = 30.0

# ACP ``prompt`` 之 ``stopReason``：本轮自然结束。
STOP_REASON_END_TURN = "end_turn"

# ACP ``prompt`` 之 ``stopReason``：本轮被取消。
STOP_REASON_CANCELLED = "cancelled"

# 工具名 → ACP ``kind`` 的启发式映射（客户端据此选图标与 UI 形态）。未命中归 ``other``；
# 这不是精确分类，是展示辅助。
_TOOL_KIND_RULES: tuple[tuple[str, str], ...] = (
    ("bash", "execute"),
    ("shell", "execute"),
    ("command", "execute"),
    ("terminal", "execute"),
    ("write", "edit"),
    ("edit", "edit"),
    ("patch", "edit"),
    ("read", "read"),
    ("cat", "read"),
    ("grep", "search"),
    ("glob", "search"),
    ("search", "search"),
    ("list", "search"),
    ("fetch", "fetch"),
    ("http", "fetch"),
    ("request", "fetch"),
)

_TEXT_DELTA = "TEXT_MESSAGE_CONTENT"

_THINKING_DELTA = "THINKING_TEXT_MESSAGE_CONTENT"

_TOOL_CALL_START = "TOOL_CALL_START"

_TOOL_CALL_ARGS = "TOOL_CALL_ARGS"

_TOOL_CALL_END = "TOOL_CALL_END"

_TOOL_CALL_RESULT = "TOOL_CALL_RESULT"

_RUN_FINISHED = "RUN_FINISHED"

# AG-UI 的**结构/生命周期事件**——纯边界标记，不携带增量内容，本解释器**有意**不映射。
# 跳过它们不是异常，因此只记 debug；真正未知的类型才值得 info。
_LIFECYCLE_EVENTS = frozenset(
    {
        "RUN_STARTED",
        "RUN_ERROR",
        "STEP_STARTED",
        "STEP_FINISHED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_END",
        "THINKING_TEXT_MESSAGE_START",
        "THINKING_TEXT_MESSAGE_END",
        "MESSAGES_SNAPSHOT",
        "STATE_SNAPSHOT",
        "STATE_DELTA",
        "CUSTOM",
    }
)

_INTERRUPT_REASONS = {
    "aidev:tool_approval": "审批请求",
    "aidev:user_question": "需要你回答问题",
}

# UserQuestion 中断的 reason（用于取出 ``metadata.questions``）。
_USER_QUESTION_REASON = "aidev:user_question"

# ``_meta`` 下本扩展使用的命名空间键。
META_KEY = "aidev"

# agent → client：hook 裁决往返（请求）。
EXT_HOOK = "aidev/hook"

# client → agent：中断恢复（请求）。
EXT_RESUME = "aidev/resume"

# agent → client：hook 降级（通知）。
EXT_HOOK_DEGRADED = "aidev/hook_degraded"


logger = getLogger(__name__)


def translate_runtime_fault(method):
    """Translate application admission failures at the protocol boundary."""

    @wraps(method)
    async def translated(*args, **kwargs):
        try:
            return await method(*args, **kwargs)
        except RuntimeFault as exc:
            rpc_code = (
                -32602 if exc.code in {"input.invalid", "resume_payload_unsupported", "hook.value_invalid"} else -32603
            )
            raise RequestError(
                rpc_code, exc.message, {"code": exc.code, "message": exc.message, "detail": exc.detail}
            ) from exc

    return translated


def _tool_kind(name: str) -> str:
    """把 AG-UI 工具名启发式映射到 ACP ``kind``。"""
    lowered = name.lower()
    for needle, kind in _TOOL_KIND_RULES:
        if needle in lowered:
            return kind

    return "other"


def _text_of_content_blocks(prompt: Any) -> str:
    """拼接字符串及 text 内容块；未宣告多媒体能力，其他块忽略。"""
    if not isinstance(prompt, list):
        return ""

    parts: list[str] = []
    for block in prompt:
        if isinstance(block, str):
            parts.append(block)
            continue
        # ``TextContentBlock``（pydantic 模型）与字典两种形态都接受。
        block_type = getattr(block, "type", None)
        if block_type is None and isinstance(block, dict):
            block_type = block.get("type")
        if block_type != "text":
            continue
        text = getattr(block, "text", None)
        if text is None and isinstance(block, dict):
            text = block.get("text")
        if isinstance(text, str):
            parts.append(text)

    return "".join(parts)


def _raw_input_of(args_text: str) -> dict[str, Any] | None:
    """将累积工具参数解析为 JSON 对象。

    ``TOOL_CALL_ARGS`` 增量拼起来通常正是一个对象的序列化；parse 失败（残缺/非对象）返回
    ``None``——协议层的对应处理是「不展示参数」，而不是发出会被客户端拒收的帧。
    """
    try:
        parsed = json.loads(args_text)
    except (TypeError, ValueError):
        return None

    return parsed if isinstance(parsed, dict) else None


def _interrupt_line(interrupt: Any) -> str | None:
    """把一条 interrupt 记录降级为一行人类可读文本。

    未知形状返回 ``None``（调用方跳过），绝不抛异常——降级路径本身绝不能打断运行。
    """
    if not isinstance(interrupt, dict):
        return None

    reason = interrupt.get("reason")
    label = _INTERRUPT_REASONS.get(reason, "中断")

    if reason == _USER_QUESTION_REASON:
        metadata = interrupt.get("metadata")
        questions = metadata.get("questions") if isinstance(metadata, dict) else None
        if isinstance(questions, list):
            headers = [str(q.get("header") or q.get("question") or "") for q in questions if isinstance(q, dict)]
            headers = [h for h in headers if h]
            if headers:
                return f"[{label}] " + "；".join(headers)

    return f"[{label}]"


class AguiEventInterpreter:
    """将 AG-UI payload 直接转换为 ACP 消息，累积工具参数并降级中断。

    每轮使用独立实例；未知事件仅记日志，不注入内容或打断运行。"""

    __slots__ = ("_tool_calls",)

    def __init__(self) -> None:
        self._tool_calls: dict[str, str] = {}

    # 工具族。

    def _on_tool_call_start(
        self, payload: dict[str, Any]
    ) -> list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress]:
        tool_call_id = payload.get("toolCallId")
        tool_name = payload.get("toolCallName")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            logger.info("TOOL_CALL_START without usable toolCallId; skipped")

            return []

        name = tool_name if isinstance(tool_name, str) and tool_name else tool_call_id
        self._tool_calls[tool_call_id] = ""

        return [
            ToolCallStart(tool_call_id=tool_call_id, title=name, name=name, kind=_tool_kind(name), status="in_progress")
        ]

    def _flush_tool_args(
        self, tool_call_id: Any
    ) -> list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress]:
        """END：把累积参数作为一个 ``ToolCallProgress`` 发出并清掉条目。"""
        if not isinstance(tool_call_id, str):
            return []

        args = self._tool_calls.pop(tool_call_id, None)
        if not args:
            return []

        return [ToolCallProgress(tool_call_id=tool_call_id, status="in_progress", raw_input=_raw_input_of(args))]

    def _on_tool_call_result(
        self, payload: dict[str, Any]
    ) -> list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress]:
        """RESULT：若条目仍在（END 缺席）先 flush 参数，再以 ``ToolCallProgress`` 收口。"""
        tool_call_id = payload.get("toolCallId")
        if not isinstance(tool_call_id, str) or not tool_call_id:
            return []

        updates = self._flush_tool_args(tool_call_id)

        content = payload.get("content")
        updates.append(
            ToolCallProgress(
                tool_call_id=tool_call_id,
                status="completed",
                raw_output=content if isinstance(content, str) else None,
            )
        )

        return updates

    # 中断族。

    @staticmethod
    def _interrupt_texts(
        payload: dict[str, Any],
    ) -> list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress]:
        """``RUN_FINISHED`` 的 outcome 为 interrupt 时，每条中断压成一行文本。"""
        outcome = payload.get("outcome")
        if not isinstance(outcome, dict) or outcome.get("type") != "interrupt":
            return []

        interrupts = outcome.get("interrupts")
        if not isinstance(interrupts, list):
            return []

        texts: list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress] = []
        for interrupt in interrupts:
            line = _interrupt_line(interrupt)
            if line:
                texts.append(AgentMessageChunk(content=TextContentBlock(text=line)))

        return texts

    def interpret(
        self, payload: dict[str, Any]
    ) -> list[AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress]:
        event_type = payload.get("type")

        if event_type == _TEXT_DELTA:
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                return [AgentMessageChunk(content=TextContentBlock(text=delta))]

            return []

        if event_type == _THINKING_DELTA:
            delta = payload.get("delta")
            if isinstance(delta, str) and delta:
                return [AgentThoughtChunk(content=TextContentBlock(text=delta))]

            return []

        if event_type == _TOOL_CALL_START:
            return self._on_tool_call_start(payload)

        if event_type == _TOOL_CALL_ARGS:
            tool_call_id = payload.get("toolCallId")
            delta = payload.get("delta")
            if isinstance(tool_call_id, str) and isinstance(delta, str) and tool_call_id in self._tool_calls:
                self._tool_calls[tool_call_id] += delta
            # START 缺失时忽略参数增量。
            return []

        if event_type == _TOOL_CALL_END:
            return self._flush_tool_args(payload.get("toolCallId"))

        if event_type == _TOOL_CALL_RESULT:
            return self._on_tool_call_result(payload)

        if event_type == _RUN_FINISHED:
            return self._interrupt_texts(payload)

        if event_type in _LIFECYCLE_EVENTS:
            logger.debug(f"lifecycle AG-UI event skipped (no content): {event_type!r}")
        else:
            logger.info(f"unmapped AG-UI event type={event_type!r}; no update emitted")

        return []


def session_meta(kwargs: dict[str, Any]) -> dict[str, Any]:
    """取 ``_meta.aidev``；类型不对抛 ``HeadlessError(protocol.invalid_params)``。"""
    meta = kwargs.get(META_KEY) or {}
    if not isinstance(meta, dict):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "_meta.aidev 类型不符（期望 object）", {"fields": ["_meta.aidev"]})
    return meta


def session_options_from_meta(
    meta: dict[str, Any],
    *,
    session_code: str,
    tool_factories: tuple[str, ...] = (),
) -> SessionOptions:
    """``_meta.aidev`` → ``SessionOptions``（agent / llm / hooks / username 对两个 ACP 协议同义）。"""
    agent = meta.get("agent")
    agent_code = (agent.get("agent_code") or agent.get("agentCode")) if isinstance(agent, dict) else agent
    if agent_code is not None and not isinstance(agent_code, str):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "字段 agent 类型不符", {"fields": ["agent"]})
    llm = meta.get("llm") or {}
    if not isinstance(llm, dict):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "字段 llm 类型不符（期望 object）", {"fields": ["llm"]})
    auth_headers = llm.get("auth_headers") or llm.get("authHeaders")
    return SessionOptions(
        session_code=session_code,
        agent_code=agent_code or "",
        model=llm.get("model") or "",
        username=meta.get("username") or "",
        base_url=llm.get("base_url") or llm.get("baseUrl") or "",
        auth_headers=auth_headers if isinstance(auth_headers, dict) else None,
        temperature=llm.get("temperature"),
        max_tokens=llm.get("max_tokens") or llm.get("maxTokens"),
        tool_factories=tool_factories,
    )


def outcome_meta(outcome: RunOutcome) -> dict[str, Any]:
    """一轮结果的 ``_meta.aidev``：状态 / turnId / 待恢复中断（两个 ACP 协议同形）。"""
    status = "interrupt" if outcome.status == "interrupted" else outcome.status
    meta: dict[str, Any] = {"status": status, "turnId": outcome.turn_id}
    if outcome.status == "interrupted":
        meta["interrupts"] = outcome.interrupts
    return meta


class HookTransport:
    """Per-run ACP hook request routing; cancellation and timeout release the RPC task."""

    def __init__(self, conn, session_id):
        self._conn = conn
        self._session_id = session_id

    async def __call__(self, request, token):
        params = {"sessionId": self._session_id, "requestId": request.line_id, **request.params}

        async def wait_cancel():
            while not token.cancelled:
                await asyncio.sleep(0.01)

        async def ask():
            return await self._conn.ext_method(EXT_HOOK, params)

        task = asyncio.create_task(ask())
        cancel = asyncio.create_task(wait_cancel())
        try:
            done, _ = await asyncio.wait(
                {task, cancel}, timeout=request.timeout_ms / 1000, return_when=asyncio.FIRST_COMPLETED
            )
            if token.cancelled:
                return HookReply("cancelled")
            if task not in done:
                return HookReply("timeout")
            try:
                result = task.result()
                return HookReply("reply", {"id": request.line_id, "ok": True, "result": result})
            except Exception:
                return HookReply("reply", {"id": request.line_id, "ok": False, "error": {"code": "host_failed"}})
        finally:
            task.cancel()
            cancel.cancel()
            await asyncio.gather(task, cancel, return_exceptions=True)


async def notify_hook_degraded(conn: Any, session_id: str, payload: dict[str, Any]) -> bool:
    """``hook_degraded`` CUSTOM 事件 → ``_aidev/hook_degraded`` 通知；不是该事件返回 False。"""
    if payload.get("type") != "CUSTOM" or payload.get("name") != "hook_degraded":
        return False
    value = payload.get("value") if isinstance(payload.get("value"), dict) else {}
    await conn.ext_notification(EXT_HOOK_DEGRADED, {"sessionId": session_id, **value})
    return True


class _AcpSink:
    """每轮独立的事件出口：持有解释器并发送 ACP 更新。失败由 RunOutcome 表达。"""

    __slots__ = ("_conn", "_session_id", "_interpreter")

    def __init__(self, conn: Any, session_id: str, interpreter: AguiEventInterpreter) -> None:
        self._conn = conn
        self._session_id = session_id
        self._interpreter = interpreter

    async def send(self, payload: dict[str, Any]) -> None:
        """将 AG-UI 事件转换为 ACP 消息并逐条发送 ``session/update``。"""
        if not isinstance(payload, dict):
            return
        if await notify_hook_degraded(self._conn, self._session_id, payload):
            return

        for message in self._interpreter.interpret(payload):
            if self._conn is not None:
                await self._conn.session_update(self._session_id, message)


class AcpAdapter:
    """ACP 协议翻译层；会话配置与运行状态由 RuntimeService 持有。"""

    def __init__(self, *, service: RuntimeService) -> None:
        self._service = service
        # -s / --session：首个 session/new 使用它（之后的会话照常新建）。
        self._default_session = service.defaults.session
        self._conn: Any = None
        # Transport configuration is adapter-owned; each run gets a new HookTransport.
        self._hooks = {}

    # ACP 生命周期。

    def on_connect(self, conn: Any) -> None:
        """SDK 在构造 ``AgentSideConnection`` 期间调用。保存连接，供 ``session/update`` 使用。"""
        self._conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: Any = None,
        client_info: Any = None,
        **kwargs: Any,
    ) -> Any:
        """声明最小 ACP 能力与扩展方法，不宣告未实现的会话加载或多媒体能力。

        模型选择由会话配置管理，握手不提供独立的模型覆盖入口。
        """
        logger.info(f"initialize: client requested protocol_version={protocol_version!r}")

        return InitializeResponse(
            protocol_version=PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(),
            field_meta={
                META_KEY: {
                    "protocol": "acp",
                    "agentMethods": [f"_{EXT_RESUME}"],
                    "clientMethods": [f"_{EXT_HOOK}", f"_{EXT_HOOK_DEGRADED}"],
                }
            },
        )

    @translate_runtime_fault
    async def new_session(
        self,
        cwd: str,
        additional_directories: Any = None,
        mcp_servers: Any = None,
        **kwargs: Any,
    ) -> Any:
        """建立平台会话，sessionId 即 session_code。

        客户端提供的 cwd 仅作诊断记录，不参与工作区（工作区固定为进程 cwd）；
        MCP 配置暂不支持，仅使用运行时已有配置。"""
        session_code = self._default_session or new_session_code()
        self._default_session = ""
        logger.info(f"new_session: cwd={cwd!r} session_id={session_code!r}")

        meta = session_meta(kwargs)
        options = session_options_from_meta(meta, session_code=session_code)
        raw_hooks = meta.get("hooks")
        self._hooks[session_code] = HookConfig.parse(raw_hooks) if raw_hooks is not None else None
        await self._service.new_session(options)

        if mcp_servers:
            logger.info(
                "new_session: client supplied mcp_servers ignored (minimal set; "
                "honouring client-supplied MCP servers is a later-phase decision)"
            )

        return NewSessionResponse(session_id=session_code)

    @staticmethod
    def _prompt_response(outcome: RunOutcome) -> PromptResponse:
        """统一转换终态与扩展元数据；失败由 SDK 转为 JSON-RPC error。"""
        if outcome.status in {"failed", "rejected"}:
            raise RequestError(
                -32603,
                outcome.error_message or "the agent run failed",
                {"code": outcome.error_code or "agent.internal_error", "message": outcome.error_message},
            )
        stop_reason = STOP_REASON_CANCELLED if outcome.status == "cancelled" else STOP_REASON_END_TURN
        return PromptResponse(stop_reason=stop_reason, field_meta={META_KEY: outcome_meta(outcome)})

    @translate_runtime_fault
    async def prompt(self, session_id: str, prompt: Any, **kwargs: Any) -> Any:
        """驱动一轮并等待终态：文本输入压平，失败抛异常，中断以 end_turn 和元数据返回。"""
        input_text = _text_of_content_blocks(prompt)
        sink = _AcpSink(self._conn, session_id, AguiEventInterpreter())

        handle = await self._service.start(
            RunRequest(session_code=session_id, input=ChatInput(input_text)),
            sink,
            hooks=self._hooks.get(session_id),
            hook_handler=HookTransport(self._conn, session_id),
        )
        outcome = await handle.result()

        return self._prompt_response(outcome)

    @translate_runtime_fault
    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """``_aidev/resume``：中断恢复 / 同一会话续轮（与 acp-dsh 同一扩展）。"""
        if method != EXT_RESUME:
            raise RequestError.method_not_found(f"_{method}")
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RequestError(-32602, "sessionId is required", {"code": "protocol.invalid_params"})
        sink = _AcpSink(self._conn, session_id, AguiEventInterpreter())
        payload = params.get("resume")
        hooks = self._hooks.get(session_id)
        transport = HookTransport(self._conn, session_id)
        if payload is None or isinstance(payload, dict) and "interruptId" not in payload:
            handle = await self._service.start(
                RunRequest(session_id, ChatInput(params.get("input") or (payload or {}).get("input", ""))),
                sink,
                hooks=hooks,
                hook_handler=transport,
            )
        else:
            handle = await self._service.resume(
                ResumeRequest(session_id, ResumeInterrupt(payload, params.get("input") or "")),
                sink,
                hooks=hooks,
                hook_handler=transport,
            )
        outcome = await handle.result()
        response = self._prompt_response(outcome)
        return response.model_dump(mode="json", by_alias=True, exclude_none=True)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        logger.info(f"acp: ignoring extension notification _{method}")

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """只取消目标会话；未知或已落定会话是合法 no-op。"""
        outcome = await self._service.cancel(session_id)

        if not outcome.cancelled:
            logger.info(f"cancel: no active run for session_id={session_id!r} (no-op)")

            return

        logger.info(f"cancel: requested for session_id={session_id!r} ({len(outcome.run_ids)} run(s))")

    async def load_session(self, *args: Any, **kwargs: Any) -> None:
        """**不实现**：本期不宣告 ``loadSession`` 能力，SDK 不会调用本方法。"""
        raise NotImplementedError("loadSession is not implemented (minimal ACP set)")


class _StdioTransport:
    """通过公共行读写组件传输 NDJSON，阻塞 I/O 不占用事件循环。

    协议输出流由调用方显式传入，不依赖全局 stdout；支持普通文件，坏帧跳过。
    EOF 后等待已接收请求的响应及输出静默窗口，硬上限防止退出无限阻塞。"""

    def __init__(self, stdout: Any, limit: int = 10 * 1024 * 1024) -> None:
        self._loop = asyncio.get_running_loop()
        self._reader = LineReader(
            sys.stdin.buffer, limit=limit, on_oversized=lambda: logger.info("acp: oversized frame dropped")
        )
        self._writer = LineWriter(stdout)
        self._writer_started = False
        self._closed = False
        # 只跟踪协议请求 ID，不复制 runtime 的运行状态。
        self._pending_requests: set[str | int] = set()
        self._last_activity = self._loop.time()

    async def _wait_for_quiet(self) -> None:
        """EOF 后等待请求响应落盘且输出静默；超过硬上限则交由 SDK 取消未完成请求。"""
        deadline = self._loop.time() + EOF_DRAIN_SECONDS
        while self._loop.time() < deadline:
            if (
                self._closed
                or self._writer.stopped.is_set()
                or (not self._pending_requests and self._loop.time() - self._last_activity >= EOF_QUIET_SECONDS)
            ):
                return
            await asyncio.sleep(0.01)

    async def receive(self) -> dict[str, Any] | None:
        """取下一帧；EOF 返回 ``None``。"""
        while True:
            if self._closed:
                return None
            if not self._writer_started:
                await self._writer.start()
                self._writer_started = True
            raw = await until_stopped(self._reader.receive(), self._writer.stopped)
            if raw is None:
                if not self._writer.stopped.is_set():
                    await self._wait_for_quiet()

                return None
            text = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else str(raw).strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except ValueError:
                # 不记录原文（可能含用户输入），只记长度。
                logger.info(f"acp: dropped a malformed frame ({len(raw)} bytes)")
                continue
            if isinstance(message, dict):
                request_id = message.get("id")
                if isinstance(message.get("method"), str) and isinstance(request_id, (str, int)):
                    self._pending_requests.add(request_id)
                self._last_activity = self._loop.time()
                return message
            logger.info("acp: dropped a non-object frame")

    # 写。

    async def send(self, message: dict[str, Any]) -> None:
        """写一帧 NDJSON 并 flush（ACP 客户端按行解析，攒着不发等于没发）。"""
        if self._closed:
            raise ConnectionError("protocol transport closed")
        data = json.dumps(message, separators=(",", ":")) + "\n"
        if not self._writer_started:
            await self._writer.start()
            self._writer_started = True
        if not await self._writer.write(data, wait_written=True):
            if self._writer.failure is not None:
                raise self._writer.failure
            raise ConnectionError("protocol output disconnected")
        response_id = message.get("id")
        if "method" not in message and isinstance(response_id, (str, int)):
            self._pending_requests.discard(response_id)
        self._last_activity = self._loop.time()

    async def close(self) -> None:
        self._closed = True
        self._reader.close()
        await self._writer.close()


async def _serve_until_signal(serving: Awaitable[Any]) -> None:
    """跑服务直到它自己结束或收到 SIGTERM / SIGHUP；后者取消服务，交给调用方的 finally 收尾。"""
    use_daemon_default_executor("acp-sdk")
    stop = async_shutdown_event()
    server = asyncio.ensure_future(serving)
    waiter = asyncio.ensure_future(stop.wait())
    try:
        await asyncio.wait({server, waiter}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in (server, waiter):
            if not task.done():
                task.cancel()
    if stop.is_set():
        logger.info("acp: shutdown requested by signal")
        await asyncio.gather(server, return_exceptions=True)
    else:
        server.result()


async def run_acp_server(service: RuntimeService, *, handle_signals: bool = False) -> int:
    """使用独立协议流运行服务，保留 stdout guard；退出时始终关闭 runtime。"""
    adapter = AcpAdapter(service=service)

    try:
        if handle_signals:
            await _serve_until_signal(run_agent(adapter, _StdioTransport(protocol_out())))
        else:
            await run_agent(adapter, _StdioTransport(protocol_out()))
    finally:
        await service.close_all("acp eof")

    return 0


def run_acp(service: RuntimeService) -> int:
    """ACP 协议入口（``__main__`` 组装好 service 后调用）。"""
    return asyncio.run(run_acp_server(service, handle_signals=True))