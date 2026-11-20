# -*- coding: utf-8 -*-
"""测试驱动：注入式后端（``execute_turn`` / ``ProcessWorker``）的进程内 NDJSON 驱动。

原 ``adapters/headless.py`` 的线上协议已由 ``--protocol acp-dsh`` 取代并从产品代码中删除；
这里只保留它的分发逻辑，作为 runtime 注入式后端行为用例（hook / 中断 / 并发 / 账本）的驱动，
**不是**对外协议。
"""

import asyncio
import contextlib
import json
import logging
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, TextIO

from aidev_agent_cli.utils.errors import (
    AGENT_BUILD_FAILED,
    AGENT_INTERNAL_ERROR,
    PROTOCOL_INVALID_JSON,
    PROTOCOL_INVALID_PARAMS,
    PROTOCOL_UNKNOWN_METHOD,
    HeadlessError,
)
from aidev_agent_cli.utils.process import install_shutdown_signals
from aidev_agent_cli.utils.stdio import protocol_out
from aidev_agent_cli.utils.runs import ResumeRequest, RunRequest, SessionOptions
from aidev_agent_cli.utils.errors import RuntimeFault
from aidev_agent_cli.utils.inputs import ChatInput, ContextInput, ResumeInterrupt
from aidev_agent_cli.hooks.models import HookReply
from aidev_agent_cli.runtime.service import CANCEL_GRACE_SECONDS

METHOD_CREATE = "create"
METHOD_TURN = "turn"
METHOD_RESUME = "resume"
METHOD_CANCEL = "cancel"
METHOD_CLOSE = "close"

#: agent → host 的 hook 请求行方法名（不在 ``SUPPORTED_METHODS`` 内：它不是宿主请求）
METHOD_HOOK = "hook"

SUPPORTED_METHODS = frozenset(
    {
        METHOD_CREATE,
        METHOD_TURN,
        METHOD_RESUME,
        METHOD_CANCEL,
        METHOD_CLOSE,
    }
)


@dataclass
class Request:
    """一条解析后的请求行。"""

    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)


def parse_request(line: str) -> Request:
    """把一行请求文本解析为 :class:`Request`。

    :raises HeadlessError: ``protocol.invalid_json``（非法 JSON）、
        ``protocol.invalid_params``（缺字段或类型不符）、``protocol.unknown_method``
        （方法不在五方法集合内）。错误信息只带字段名，不回显请求体。
    """
    try:
        payload = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise HeadlessError(PROTOCOL_INVALID_JSON, f"请求行不是合法 JSON：{type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise HeadlessError(PROTOCOL_INVALID_JSON, "请求行必须是 JSON 对象")

    request_id = payload.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "id 缺失或类型不符（期望非空 str）", {"fields": ["id"]})

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "method 缺失或类型不符（期望非空 str）", {"fields": ["method"]})
    if method not in SUPPORTED_METHODS:
        raise HeadlessError(
            PROTOCOL_UNKNOWN_METHOD,
            f"未知方法：{method}",
            {"method": method, "supported": sorted(SUPPORTED_METHODS)},
        )

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "params 类型不符（期望 object）", {"fields": ["params"]})

    return Request(id=request_id, method=method, params=params)


def require_param(
    params: dict[str, Any], name: str, expected: type | tuple[type, ...] = str, *, optional: bool = False
):
    """取出必填参数并校验类型；缺失或类型不符时抛 ``protocol.invalid_params``。"""
    value = params.get(name)
    if value is None:
        if optional:
            return None
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, f"缺少必填字段：{name}", {"fields": [name]})
    if not isinstance(value, expected):
        raise HeadlessError(
            PROTOCOL_INVALID_PARAMS,
            f"字段 {name} 类型不符（期望 {getattr(expected, '__name__', expected)}）",
            {"fields": [name]},
        )
    return value


def encode_event(request_id: str, event: Any) -> str:
    """把一条 AG-UI 事件编码为事件行。

    事件可以是 SDK 模型，也可以是线上 JSON 载荷（``utils.terminal`` 产出的终止事件就是
    纯字典——父进程不装载 SDK，形状由 ``model_dump(...)`` 等价保证）。
    """
    payload = event if isinstance(event, dict) else event.model_dump(mode="json", by_alias=True)

    return json.dumps({"id": request_id, "type": "event", "event": payload}, ensure_ascii=False)


def encode_hook_request(line_id: str, params: dict[str, Any]) -> str:
    """把一次 hook 请求编码为 agent → host 的请求行（REQ-18）。

    ``params`` 固定四键：``session_code`` / ``turn_id`` / ``point`` / ``payload``；
    ``line_id`` 形如 ``<本轮请求 id>#h<n>``，宿主据此原样回填答复行的 ``id``。
    """
    return json.dumps({"id": line_id, "type": "request", "method": METHOD_HOOK, "params": params}, ensure_ascii=False)


def is_hook_reply(payload: Any) -> bool:
    """判断一行入站 JSON 是否为宿主答复行（``ok`` 在、``method`` 不在，REQ-18）。"""
    return isinstance(payload, dict) and "ok" in payload and "method" not in payload


def encode_result(request_id: str, result: dict[str, Any]) -> str:
    """编码成功响应行。"""
    return json.dumps({"id": request_id, "ok": True, "result": result}, ensure_ascii=False)


def encode_error(request_id: str, error: HeadlessError) -> str:
    """编码失败响应行（不含完整请求体与凭据）。"""
    return json.dumps({"id": request_id, "ok": False, "error": error.to_payload()}, ensure_ascii=False)


# ============================== 参数辅助 ==============================


def _lenient_request_id(line: str) -> str:
    """协议解析失败时尽量取回请求 id，便于宿主机按 id 聚合错误。"""
    payload = _try_json(line)
    if isinstance(payload, dict) and isinstance(payload.get("id"), str):
        return payload["id"]
    return ""


def _try_json(line: str) -> Any:
    """尽量把一行解析成 JSON 对象；失败返回 ``None``（不抛错、不影响进程可用性）。"""
    try:
        return json.loads(line)
    except (ValueError, TypeError):
        return None


def _optional_str(container: dict[str, Any], name: str) -> str | None:
    value = container.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, f"字段 {name} 类型不符（期望 str）", {"fields": [name]})
    return value


def _optional_messages(params: dict[str, Any]) -> list[dict] | None:
    value = params.get("messages")
    if value is None:
        return None
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise HeadlessError(
            PROTOCOL_INVALID_PARAMS, "字段 messages 类型不符（期望 dict 列表）", {"fields": ["messages"]}
        )
    return value


class _DriverHooks:
    """测试协议的进程级 hook：配置来自首个 create，应答走本 AguiAdapter 的 hook 请求行。"""

    def __init__(self, config: Any, reply: Any) -> None:
        self.config = config
        self._reply = reply

    async def __call__(self, request: Any, token: Any) -> Any:
        return await self._reply(request, token)


def _optional_hooks(params: dict[str, Any]) -> Any:
    """解析 ``create.params.hooks``；不传即完全关闭 hook 层（REQ-19）。"""
    from aidev_agent_cli.sdk.hooks import HookConfig

    value = params.get("hooks")
    if value is None:
        return None
    return HookConfig.parse(value)


def _optional_tools(params: dict[str, Any]) -> list | None:
    """``create.params.tools``：进程内注入工具（仅嵌入式 / 测试接缝，不是协议承诺）。"""
    value = params.get("tools")
    if value is None:
        return None
    if not isinstance(value, list):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "字段 tools 类型不符（期望工具列表）", {"fields": ["tools"]})
    return value


logger = logging.getLogger("aidev_agent_cli")


# ============================== 日志与前置校验 ==============================


def configure_logging() -> None:
    """把本包的诊断指向 stderr（文件日志由 ``utils.logging`` 在导入时配好）。

    只处理"标准库 logging 的 root 与 loguru 也改道 stderr"这一件事。语义与合并前一致：
    stdout 只承载协议行，任何诊断都不得落进去。
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    if root.level == logging.NOTSET or root.level < logging.INFO:
        root.setLevel(logging.INFO)

    # loguru（若被安装）同样改道 stderr
    try:
        from loguru import logger as loguru_logger

        loguru_logger.remove()
        loguru_logger.add(sys.stderr, level="INFO")
    except ImportError:  # pragma: no cover - loguru 是测试依赖，运行时可不装
        pass


def assert_in_memory_message_handler() -> None:
    """取消依赖内存消息处理器；非内存实现时显式失败退出（REQ-10）。"""
    from aidev_agent.services.messages_handler.factory import message_handler_factory
    from aidev_agent.services.messages_handler.in_memory import InMemoryQueueMessageHandler

    handler = message_handler_factory()
    if not isinstance(handler, InMemoryQueueMessageHandler):
        raise SystemExit(
            f"aidev_agent_cli 需要内存消息处理器（InMemoryQueueMessageHandler），"
            f"当前为 {type(handler).__name__}；请设置 MESSAGE_HANDLER_TYPE=inmemory"
        )


# ============================== 分发 ==============================


class HostReplies:
    """Test host correlation only; the application receives a per-run hook handler."""

    def __init__(self):
        self.pending = {}
        self.lock = threading.Lock()

    def register(self, line_id):
        from types import SimpleNamespace
        slot = SimpleNamespace(event=threading.Event(), payload=None)
        with self.lock:
            self.pending[line_id] = slot
        return slot

    def deliver(self, payload):
        with self.lock:
            slot = self.pending.get(payload.get("id"))
            if slot is not None:
                slot.payload = payload
                slot.event.set()

    def discard(self, line_id):
        with self.lock:
            self.pending.pop(line_id, None)


class AguiAdapter:
    """NDJSON → ``RuntimeService`` 的翻译器。

    **不持第二份运行状态**：``turn`` / ``resume`` 都走 ``service.start`` / ``service.resume``，
    然后在本线程里 ``await handle.result()`` 拿 ``RunOutcome``；``cancel`` 走
    ``service.cancel``，``close`` 走 ``service.close``。合并前这里是 ``SessionRegistry`` +
    ``Session.cancel`` 各持一份取消状态。

    ``turn`` / ``resume`` 仍走**独立工作线程**（协议要求主线程继续读 ``cancel``/``close``），
    线程里跑一个私有事件循环把 service 的协程跑完。``ev 线程桥`` 与注入式后端的同步语义
    因此都保留。
    """

    def __init__(self, *, stream: TextIO, service: Any = None, defaults: Any = None) -> None:
        from aidev_agent_cli.utils.runs import LaunchOptions

        self._stream = stream
        #: 命令行默认值（-m / -s / --resources）。
        self._defaults = defaults or LaunchOptions()
        self._write_lock = threading.Lock()
        self.hook_router = HostReplies()
        #: hook 是 RuntimeService 的进程级依赖；本协议的 hooks 写在 create 上，所以 service
        #: 延迟到第一次 create 再组装（同一 AguiAdapter 内各会话必须使用同一份 hooks 配置）。
        self._service = service
        self._service_hooks: Any = None
        self._transports = {}
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()
        #: ``session_code -> 该会话的事件循环``（长期存活，跑在后台线程里）。
        #: 同会话的每一轮、它的取消后备、以及后续轮次的排队等待都必须在**同一个**循环上：
        #: 否则 ``handle.result()`` 的 future 属于别的循环（``await wasn't used with future``），
        #: 且 ``Task.cancel()`` 跨循环静默失效。这是线程桥的簿记，不是运行状态——运行状态在
        #: service。
        self._loops: dict[str, Any] = {}
        self._loop_threads: dict[str, threading.Thread] = {}
        self._loops_lock = threading.Lock()
        #: 同会话轮次的**排队锁**：只覆盖"等上一轮落定 → 登记本轮 → 投递执行"这三步，
        #: 不覆盖执行本身（执行期间其他会话照常并发）。跨会话不竞争同一把锁的语义不变——
        #: 锁内唯一的阻塞是 ``_await_previous_turn``，它只对**同会话**有等待。
        self._queue_lock = threading.Lock()

    # ---------------- 会话视图 ----------------

    @property
    def registry(self) -> "ServiceRegistryView":
        """``session_code -> Session`` 的**只读视图**（派生自 ``RuntimeService``）。

        它存在的理由是**不要第二份登记表**：合并前这里是一个真的 ``SessionRegistry``，与
        service 的索引并存，于是"会话在哪"有两个答案。现在视图每次现查 service
        （``environment(session_code)``，未知会话抛 ``session.not_found``，与合并前一致），
        没有自己的字典。
        """
        return ServiceRegistryView(self)

    # ---------------- 行入口 ----------------

    def handle_line(self, line: str) -> None:
        payload = _try_json(line)
        if is_hook_reply(payload):
            # 答复行：投递给等待方，不产生响应行、不进请求解析、不报未知方法（REQ-18）
            self.hook_router.deliver(payload)
            return
        try:
            request = parse_request(line)
        except (HeadlessError, RuntimeFault) as exc:
            self._emit_error_line(_lenient_request_id(line), exc)
            return
        self.handle_request(request)

    def handle_request(self, request: Request) -> None:
        """处理一条已解析的请求（进程内调用可携带非 JSON 参数，如 ``event_sink``）。"""
        if request.method in (METHOD_TURN, METHOD_RESUME):
            try:
                admission = self._admit_request(request)
            except (HeadlessError, RuntimeFault) as exc:
                self._respond_error(request.id, exc)
                return
            worker = threading.Thread(
                target=self._run_session_task,
                args=(request, admission),
                name=f"headless-{request.method}-{request.id}",
                daemon=False,
            )
            with self._workers_lock:
                self._workers.append(worker)
            worker.start()
            return

        self._handle_sync(request)

    def join(self, timeout: float | None = None) -> None:
        with self._workers_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout)

    def close_all(self, reason: str = "eof") -> None:
        """EOF / 关闭路径：有界等待 → 取消 → 关 owned saver → flush。幂等。

        合并前 ``serve()`` 在这里只做 ``join()`：不取消活跃轮、不关 checkpointer（§0.4 的
        第一个真实缺陷）。现在交给 service 统一处理。
        """
        codes = asyncio.run_coroutine_threadsafe(self.service.list_sessions(), self._session_loop("__runtime__")).result()
        for record in codes:
            session_code = record["sessionCode"]
            asyncio.run_coroutine_threadsafe(
                self.service.close(session_code), self._session_loop(session_code),
            ).result()
        loop = self._session_loop("__shutdown__")
        asyncio.run_coroutine_threadsafe(self.service.close_all(reason), loop).result()
        self._stop_loops()

    # ---------------- 各方法 ----------------

    def _handle_sync(self, request: Request) -> None:
        try:
            if request.method == METHOD_CREATE:
                result = self._create(request)
            elif request.method == METHOD_CANCEL:
                result = self._cancel(request)
            elif request.method == METHOD_CLOSE:
                result = self._close(request)
            else:  # pragma: no cover - parse_request 已拦截未知方法
                raise HeadlessError(PROTOCOL_INVALID_PARAMS, f"未知方法：{request.method}")
        except (HeadlessError, RuntimeFault) as exc:
            self._respond_error(request.id, exc)
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("headless request failed: method=%s", request.method)
            self._respond_error(
                request.id,
                HeadlessError(AGENT_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"),
            )
            return
        self._write(encode_result(request.id, result))

    def _run_session_task(self, request: Request, admission: Any) -> None:
        """工作线程里跑一轮：把本轮投递到**该会话的事件循环**并等它落定。"""
        try:
            outcome = self._drive(admission)
        except (HeadlessError, RuntimeFault) as exc:
            self._respond_error(request.id, exc)
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("headless %s failed: id=%s", request.method, request.id)
            self._respond_error(
                request.id,
                HeadlessError(AGENT_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"),
            )
            return

        self._write_outcome(request.id, outcome)

    def _admit_request(self, request: Request) -> Any:
        """把 ``turn`` / ``resume`` 翻译成一次 service 调用并等它落定。

        **每会话一个事件循环**（:meth:`_session_loop`）：本轮的 ``asyncio.Task``、它的取消
        后备、以及同会话后续轮次的调度都在**同一个**循环上。给每轮新建循环会让
        ``handle.result()`` 的 future 属于别的循环（``await wasn't used with future``），
        也会让 ``Task.cancel()`` 跨循环静默失效。

        **同会话串行**在 ``_queue_lock`` 下完成"等上一轮落定 → 登记本轮 → 投递执行"这三步：
        只锁这一小段，执行期间其他会话照常并发。合并前的等价语义来自 ``Session``
        的会话锁（排队串行），这里把它前移到 adapter，因为统一后的运行登记在 service 上。
        """
        session_code = require_param(request.params, "session_code")
        self._ensure_session(session_code)

        text = _optional_str(request.params, "input") or ""
        messages = _optional_messages(request.params)
        if request.method == METHOD_RESUME and request.params.get("resume") is not None:
            run_request = ResumeRequest(
                session_code, ResumeInterrupt(request.params["resume"], text), correlation_id=request.id,
            )
            starter = self.service.resume
        else:
            run_request = RunRequest(
                session_code, ContextInput(messages, text) if messages else ChatInput(text), correlation_id=request.id,
            )
            starter = self.service.start
        loop = self._session_loop(session_code)
        with self._queue_lock:
            sink = self._transports[session_code]
            handle = asyncio.run_coroutine_threadsafe(
                starter(run_request, sink or self._event_sink(request.id)), loop,
            ).result()

        return loop, handle

    def _drive(self, admission: Any) -> Any:
        loop, handle = admission
        async def _wait() -> Any:
            return await handle.result()

        return asyncio.run_coroutine_threadsafe(_wait(), loop).result()

    def _session_loop(self, session_code: str) -> asyncio.AbstractEventLoop:
        """取（或创建并启动）该会话的事件循环，放在后台线程里跑。

        ``_loops`` / ``_loop_threads`` / ``_loops_lock`` 是线程桥的簿记（不是运行状态）：
        运行状态在 ``RuntimeService``。循环随会话生命周期存活，``close_all`` 之后统一停掉。
        """
        session_code = "__runtime__"
        with self._loops_lock:
            loop = self._loops.get("__runtime__")
            if loop is not None and not loop.is_closed():
                return loop

            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name=f"headless-loop-{session_code}",
                daemon=True,
            )
            thread.start()
            self._loops[session_code] = loop
            self._loop_threads[session_code] = thread

            return loop

    def _stop_loops(self) -> None:
        """停掉全部会话循环（``close_all`` 之后调用；幂等）。"""
        with self._loops_lock:
            loops = dict(self._loops)
            threads = dict(self._loop_threads)
            self._loops.clear()
            self._loop_threads.clear()

        for loop in loops.values():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(loop.stop)
        for thread in threads.values():
            thread.join(timeout=5)
            if thread.is_alive():
                logger.info("headless: session loop thread did not stop within 5s")

    async def _hook_reply(self, request: Any, token: Any) -> HookReply:
        slot = self.hook_router.register(request.line_id)
        try:
            self._write(encode_hook_request(request.line_id, request.params))
            deadline = asyncio.get_running_loop().time() + request.timeout_ms / 1000
            while not slot.event.is_set():
                if token.cancelled:
                    return HookReply("cancelled")
                if asyncio.get_running_loop().time() >= deadline:
                    return HookReply("timeout")
                await asyncio.sleep(0.005)
            return HookReply("reply", slot.payload)
        finally:
            self.hook_router.discard(request.line_id)

    def _event_sink(self, request_id: str) -> Any:
        """本次 run 的事件出口：把每个 AG-UI 事件写成一条带请求 id 的事件行。"""

        async def _send(event: Any) -> None:
            # 统一执行路径的 sink 收到的是线上 JSON 载荷（与 SSE 同形）；模型对象也兼容。
            if isinstance(event, dict):
                self._write(json.dumps({"id": request_id, "type": "event", "event": event}, ensure_ascii=False))
                return
            self._write(encode_event(request_id, event))

        return _send

    def _write_outcome(self, request_id: str, outcome: Any) -> None:
        """``RunOutcome`` → 响应行。失败轮 = ``ok=false``（**不**是成功）。"""
        if outcome.status == "interrupted":
            self._write(
                encode_result(
                    request_id,
                    {
                        "session_code": outcome.session_code,
                        "turn_id": outcome.turn_id,
                        "status": "interrupt",
                        "interrupts": outcome.interrupts,
                    },
                )
            )
            return

        if outcome.status in ("failed", "rejected"):
            # 事件侧已经发过 RUN_ERROR（注入式后端在失败时 ``emit_error``），因此这里只补
            # 结构化错误响应，不重复写事件——与合并前的基线序列一致。
            self._write_error_payload(
                request_id,
                {
                    "code": outcome.error_code or AGENT_INTERNAL_ERROR,
                    "message": outcome.error_message or "执行失败",
                },
            )
            return

        self._write(
            encode_result(
                request_id,
                {
                    "session_code": outcome.session_code,
                    "turn_id": outcome.turn_id,
                    "status": outcome.status,
                },
            )
        )

    def _ensure_session(self, session_code: str) -> None:
        """校验会话存在且未关闭；两种情况各有稳定错误码（与合并前逐字一致）。

        ``session.closed`` 从会话后端读（``ensure_open`` 抛它）；``session.not_found`` 从
        service 索引读。分开是因为**关闭过**与**从未存在**对宿主是两件事：前者可以重建
        同名会话，后者说明请求写错了。
        """
        info = asyncio.run_coroutine_threadsafe(
            self.service.session_info(session_code), self._session_loop(session_code),
        ).result()
        environment = info
        if environment is None:
            from aidev_agent_cli.utils.errors import SESSION_NOT_FOUND

            raise HeadlessError(SESSION_NOT_FOUND, f"未知会话：{session_code}", {"session_code": session_code})

        if environment.closed:
            raise HeadlessError("session.closed", "会话已关闭", {"session_code": session_code})

    def _create(self, request: Request) -> dict:
        params = request.params
        if not params.get("session_code") and self._defaults.session:
            params = {**params, "session_code": self._defaults.session}
        session_code = require_param(params, "session_code")
        agent = params.get("agent")
        if isinstance(agent, str):
            agent_code = agent
        elif isinstance(agent, dict):
            agent_code = require_param(agent, "agent_code")
        else:
            raise HeadlessError(
                PROTOCOL_INVALID_PARAMS,
                "缺少必填字段：agent（期望 agent_code 或含 agent_code 的对象）",
                {"fields": ["agent"]},
            )

        llm = params.get("llm") or {}
        if not isinstance(llm, dict):
            raise HeadlessError(PROTOCOL_INVALID_PARAMS, "字段 llm 类型不符（期望 object）", {"fields": ["llm"]})

        base_url = _optional_str(llm, "base_url") or ""
        if not (_optional_str(llm, "model") or self._defaults.model):
            raise HeadlessError(AGENT_BUILD_FAILED, "注入式会话缺少模型配置")

        options = SessionOptions(
            resources=("injected",),
            session_code=session_code,
            agent_code=agent_code,
            model=_optional_str(llm, "model") or "",
            username=_optional_str(params, "username") or "",
            base_url=base_url,
            auth_headers=llm.get("auth_headers") if isinstance(llm.get("auth_headers"), dict) else None,
            temperature=llm.get("temperature"),
            max_tokens=llm.get("max_tokens"),

            tool_factories=tuple(params.get("tool_factories", ())) + tuple(
                f"{tool.func.__module__}:{tool.name}_factory" for tool in (_optional_tools(params) or [])
            ) + (("tests.integration._worker_observations:record_builds",) if params.get("record_builds") else ()),

        )

        self._bind_hooks(_optional_hooks(params))
        self._transports[session_code] = params.get("event_sink")
        loop = self._session_loop(session_code)
        async def create():
            await self.service.new_session(options)
            return await self.service.prepare(session_code)

        outcome = asyncio.run_coroutine_threadsafe(create(), loop).result()
        if not outcome.ok:
            asyncio.run_coroutine_threadsafe(self.service.close(session_code), loop).result()
            raise HeadlessError(outcome.error_code or AGENT_BUILD_FAILED, outcome.error_message)

        return {"session_code": session_code, "created": True}

    @property
    def service(self) -> Any:
        if self._service is None:
            self._service = self._build_service(None)
        return self._service

    def _build_service(self, hooks: Any) -> Any:
        from aidev_agent_cli.runtime.process_config import ProcessConfig
        from aidev_agent_cli.runtime.service import RuntimeService

        self._service_hooks = hooks
        return RuntimeService(
            config=ProcessConfig(launch=self._defaults),
            busy_mode="queue",
            hooks=_DriverHooks(hooks, self._hook_reply) if hooks is not None else None,
        )

    def _bind_hooks(self, hooks: Any) -> None:
        """首个 create 决定进程级 hooks；之后的 create 只能与它一致。"""
        if self._service is None:
            self._service = self._build_service(hooks)
            return
        current = self._service_hooks.snapshot() if self._service_hooks is not None else None
        wanted = hooks.snapshot() if hooks is not None else None
        if current != wanted:
            raise HeadlessError(PROTOCOL_INVALID_PARAMS, "同一进程内各会话的 hooks 配置必须一致", {"fields": ["hooks"]})

    def _cancel(self, request: Request) -> dict:
        """请求取消当前轮。**在运行所在的事件循环上**执行（取消是循环内动作）。

        为什么必须回到那个循环：本轮的任务（``service.start`` 起的 ``asyncio.Task``）与它的
        取消后备都绑定在 ``_drive`` 里那个循环上。在新循环上调 ``service.cancel`` 会
        ``asyncio.create_task`` 到一个不属于本轮的循环：``Task.cancel()`` 会跨循环调度失败，
        后备永远不跑，取消就静默失效（实测表现：取消后本轮仍以 ``finished`` 收尾）。

        标志本身是 ``threading.Event``，跨线程安全；这里要的是"在**本轮循环**里安排动作"，
        因此经 ``asyncio.run_coroutine_threadsafe`` 投递。

        没有活跃轮时 ``cancelled=True`` 是既有契约（"该会话当前没有被取消的东西"），语义不变。
        """
        session_code = self._session_code_for(request)
        loop = self._loops.get(session_code)
        if loop is None or loop.is_closed():
            # 没有活跃轮的循环：直接翻转（``service.cancel`` 对未知/已落定运行是 no-op）。
            self._cancel_sync(session_code)

            return {"session_code": session_code, "cancelled": True}

        future = asyncio.run_coroutine_threadsafe(self.service.cancel(session_code), loop)
        future.result(timeout=CANCEL_GRACE_SECONDS + 5)

        return {"session_code": session_code, "cancelled": True}

    def _cancel_sync(self, session_code: str) -> None:
        """没有可投递的循环时：翻 token（``cancel`` 的同步等价，用于收尾/测试路径）。"""
        for handle in self.service.run_handles().get(session_code, {}).values():
            handle.token.cancel()

    def _close(self, request: Request) -> dict:
        # 幂等：关闭一个不存在的会话是成功（退出清理路径不得因未知 code 失败）。
        session_code = require_param(request.params, "session_code")
        outcome = asyncio.run_coroutine_threadsafe(
            self.service.close(session_code), self._session_loop(session_code),
        ).result()

        return {
            "session_code": session_code,
            "closed": True,
            "already_closed": outcome.already_closed,
        }

    def _session_code_for(self, request: Request) -> str:
        session_code = require_param(request.params, "session_code")
        self._ensure_session(session_code)

        return session_code

    # ---------------- 写出 ----------------

    def _write(self, line: str) -> None:
        with self._write_lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def _respond_error(self, request_id: str, error: HeadlessError) -> None:
        """先写一条 RUN_ERROR 事件，再写结构化错误响应。"""
        from aidev_agent_cli.utils.terminal import build_error_event

        self._write(encode_event(request_id, build_error_event(error.message)))
        self._write(encode_error(request_id, HeadlessError(error.code, error.message, error.detail)))

    def _write_error_payload(self, request_id: str, payload: dict) -> None:
        error = HeadlessError(
            payload.get("code") or AGENT_INTERNAL_ERROR,
            payload.get("message") or "执行失败",
            payload.get("detail"),
        )
        self._write(encode_error(request_id, error))

    def _emit_error_line(self, request_id: str, error: HeadlessError) -> None:
        """协议层错误：仍产出事件行 + 响应行各一条（拿不到 id 时用空串）。"""
        self._respond_error(request_id, error)


# ============================== 会话视图 ==============================


class ServiceRegistryView:
    """``dispatcher.registry`` 的只读视图——**不是**第二份登记表。

    只有两个方法（``get`` / ``codes``），都直接问 ``RuntimeService``。合并前这里是完整的
    ``SessionRegistry``（自有字典 + create/remove），与 service 的索引并存；现在任何"改登记"
    都必须经 service，视图只能读。
    """

    def __init__(self, dispatcher: Any) -> None:
        self._dispatcher = dispatcher

    def get(self, session_code: str) -> Any:
        info = asyncio.run_coroutine_threadsafe(
            self._dispatcher.service.session_info(session_code), self._dispatcher._session_loop(session_code),
        ).result()
        environment = info
        if environment is None:
            from aidev_agent_cli.utils.errors import SESSION_NOT_FOUND

            raise HeadlessError(SESSION_NOT_FOUND, f"未知会话：{session_code}", {"session_code": session_code})

        return environment

    def codes(self) -> list[str]:
        return [record["sessionCode"] for record in asyncio.run_coroutine_threadsafe(
            self._dispatcher.service.list_sessions(), self._dispatcher._session_loop("__runtime__"),
        ).result()]


# ============================== 入口 ==============================


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None, *, defaults: Any = None) -> int:
    """从 ``stdin`` 读取 NDJSON 请求，向 ``stdout`` 写出事件与响应；EOF 时返回 0。

    EOF 路径**必**调 ``close_all``：合并前只 ``join()`（不取消活跃轮、不关 saver）。
    """
    stdin = stdin if stdin is not None else sys.stdin
    # 默认目标是**协议流**，不是 ``sys.stdout``：入口装过 stdout guard 之后，``sys.stdout``
    # 已经是指向 stderr 的代理——照旧绑定它会把每一行响应都写进诊断通道，宿主永远收不到。
    # ``protocol_out()`` 在 guard 未装时回退到原始 stdout，因此嵌入式调用（宿主显式传
    # ``stdout``）与直接 ``serve()`` 两种形态都成立。
    stdout = stdout if stdout is not None else protocol_out()
    dispatcher = AguiAdapter(stream=stdout, defaults=defaults)
    try:
        for raw_line in stdin:
            line = raw_line.strip()
            if not line:
                continue
            dispatcher.handle_line(line)
        dispatcher.join()
    finally:
        dispatcher.close_all("headless eof")

    return 0


def main(argv: list[str] | None = None, *, defaults: Any = None) -> int:
    configure_logging()
    assert_in_memory_message_handler()

    # SIGTERM / SIGHUP：在主线程抛出 _ShutdownRequested，打断阻塞的 stdin 读取；
    # serve() 的 finally 随之执行 close_all（与 EOF 同一条收尾路径）。
    def _interrupt() -> None:
        raise _ShutdownRequested

    install_shutdown_signals(_interrupt)

    try:
        return serve(defaults=defaults)
    except _ShutdownRequested:
        logger.info("headless: shutdown requested by signal")

        return 0


class _ShutdownRequested(Exception):
    """信号请求关闭（仅在 headless 主线程内抛出并捕获）。"""