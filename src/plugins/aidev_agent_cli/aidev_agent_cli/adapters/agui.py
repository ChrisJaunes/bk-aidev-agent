"""AGUI 的 NDJSON 协议适配器：帧封装、请求分发和 stdio 运行入口。

启动前由 __main__ 安装 stdout guard 和 sys.path 加固，避免 SDK 导入污染协议输出。
请求和事件通过 id / runId 关联，不依赖行顺序；payload 原样承载 SDK 的 AG-UI 事件。
行缓冲由 LineReader 维护。stdin EOF 或关闭请求触发运行时清理，随后关闭输出。
"""

from __future__ import annotations

import asyncio
import json
import sys
from logging import getLogger
from typing import Any

from ..infrastructure.process import async_shutdown_event, use_daemon_default_executor
from ..infrastructure.startup import install_startup_hooks
from ..infrastructure.stdio import protocol_out
from ..infrastructure.streams import LineReader, LineWriter, until_stopped
from ..runtime.contracts import ResumeRequest, RunRequest, SessionOptions, new_session_code
from ..runtime.errors import RuntimeFault
from ..runtime.hook_contract import sdk_package_path
from ..runtime.inputs import ChatInput, ResumeInterrupt
from ..runtime.service import build_runtime

# 本模块**不导入 SDK**：ready 帧的 sdk_file 是一个纯诊断字段，经这一处按需查询
# （见 :func:`aidev_agent_cli.runtime.hook_contract.sdk_package_path`）。
sdk_file = sdk_package_path

# 每个协议帧的 v 字段统一使用此版本。
PROTOCOL_VERSION = 1
READY_ID = "boot"
# 能力标志供调用方判断支持的方法；文件资源通过 agent.info 返回。
DEFAULT_CAPABILITIES = ["chat", "cancel", "session", "agent", "auth", "config"]

_QUEUE_MAX = 256
_ENQUEUE_TIMEOUT_SECONDS = 30.0

# 固定日志命名空间，保留诊断使用的 [runtime] 标签。
logger = getLogger("aidev_agent_cli.tui.runtime")


class FrameWriter(LineWriter):
    """AGUI 帧序列化适配层；send 成功表示入队，关闭时等待写出。
    协议形态：
    Node → Python::
        {"v":1,"id":"req-1","method":"session.start","params":{"sessionCode":"s1","input":"你好"}}
        {"v":1,"id":"req-2","method":"session.cancel","params":{"runId":"run-1"}}
        {"v":1,"id":"req-4","method":"process.shutdown","params":{}}

    Python → Node::
        {"v":1,"id":"boot","kind":"ready","capabilities":["chat","cancel","session"]}
        {"v":1,"id":"req-1","kind":"started","runId":"run-1"}
        {"v":1,"id":"req-1","kind":"event","runId":"run-1","payload":{...AG-UI event...}}
        {"v":1,"id":"req-1","kind":"completed","runId":"run-1"}
        {"v":1,"id":"req-3","kind":"result","payload":{"items":[]}}
        {"v":1,"id":"req-1","kind":"error","error":{"code":"AGENT_ERROR","message":"..."}}
    """

    def __init__(self, stream: Any, *, maxsize: int = _QUEUE_MAX) -> None:
        super().__init__(stream, maxsize=maxsize, write_timeout=_ENQUEUE_TIMEOUT_SECONDS)

    async def send(self, frame: dict[str, Any]) -> bool:
        return await self.write(json.dumps(frame, ensure_ascii=False) + "\n")

    async def send_ready_frame(self, capabilities: list[str] | None = None, *, sdk_file: str | None = None) -> bool:
        """声明 worker 已就绪；sdk_file 单独报告 SDK 路径，未提供时省略该键。"""
        frame: dict[str, Any] = {
            "v": PROTOCOL_VERSION,
            "id": READY_ID,
            "kind": "ready",
            "capabilities": list(DEFAULT_CAPABILITIES if capabilities is None else capabilities),
        }
        if sdk_file is not None:
            frame["sdk_file"] = sdk_file
        return await self.send(frame)

    async def send_started_frame(self, req_id: str, run_id: str) -> bool:
        """确认 ``session.start``，并将用于关联的 ``runId`` 交给调用方。"""
        return await self.send({"v": PROTOCOL_VERSION, "id": req_id, "kind": "started", "runId": run_id})

    async def send_event_frame(self, req_id: str, run_id: str, payload: dict[str, Any]) -> bool:
        """承载运行中的 agent 产生的一个原始 AG-UI 事件。"""
        return await self.send(
            {"v": PROTOCOL_VERSION, "id": req_id, "kind": "event", "runId": run_id, "payload": payload}
        )

    async def send_completed_frame(self, req_id: str, run_id: str) -> bool:
        """表示一次运行正常结束。"""
        return await self.send({"v": PROTOCOL_VERSION, "id": req_id, "kind": "completed", "runId": run_id})

    async def send_result_frame(self, req_id: str, payload: dict[str, Any]) -> bool:
        """回应非流式请求（``session.*``、``process.shutdown`` 等）。"""
        return await self.send({"v": PROTOCOL_VERSION, "id": req_id, "kind": "result", "payload": payload})

    async def send_error_frame(self, req_id: str | None, code: str, message: str) -> bool:
        """针对 ``req_id`` 上报一次失败。

        对于无法归因到单个请求的失败，``req_id`` 可以为 ``None``——例如无法读取的行上的
        解析错误，或在读循环开始前就检测到的导入失败。
        """
        return await self.send(
            {"v": PROTOCOL_VERSION, "id": req_id, "kind": "error", "error": {"code": code, "message": message}}
        )


class RunEventSink:
    """每次运行独立的事件出口，为事件附加 request ID 和 run ID。

    peer_gone 透传 writer 的对端状态，供运行判断连接是否关闭。
    """

    __slots__ = ("_writer", "_req_id", "run_id")

    def __init__(self, writer: Any, req_id: str) -> None:
        self._writer = writer
        self._req_id = req_id
        self.run_id = ""

    @property
    def peer_gone(self) -> bool:
        return bool(getattr(self._writer, "peer_gone", False))

    async def send(self, payload: dict[str, Any]) -> None:
        await self._writer.send_event_frame(self._req_id, self.run_id, payload)


def _record(info: Any) -> dict[str, str] | None:
    """会话身份记录；未知或已关闭时 ``None``（``session.get`` 从不创建）。"""
    if info is None or info.closed:
        return None
    return info.record()


class Dispatcher:
    """协议帧路由与运行结果翻译；会话和运行状态由 service 持有。

    runs / cancelled 是运行句柄的派生视图，写入只经 service。
    """

    def __init__(
        self,
        writer: Any,
        service: Any,
        *,
        shutdown_cb: Any = None,
    ) -> None:
        self.writer = writer
        self._shutdown_cb = shutdown_cb
        self._shutting_down = False
        # 进程级运行时由调用方组装。
        self.service = service

    async def _default_session(self) -> str:
        code = await self.service.latest_session_code()
        if code is not None:
            return code
        code = self.service.defaults.session or new_session_code()
        info = await self.service.new_session(SessionOptions(session_code=code))
        return info.session_code

    # 运行状态的派生视图。

    @property
    def runs(self) -> dict[str, asyncio.Task[Any]]:
        """运行句柄的 Task 视图，包含尚未被 service 清理的已落定句柄。

        保留短窗口，确保对端收到 started 后仍能查询对应的 run ID。
        """
        out: dict[str, asyncio.Task[Any]] = {}
        for bucket in self.service.run_handles().values():
            for run_id, handle in bucket.items():
                if handle.task is not None:
                    out[run_id] = handle.task

        return out

    @property
    def cancelled(self) -> set[str]:
        """已请求取消的 run_id（**派生**自句柄的 CancelToken）。"""
        return {
            run_id
            for bucket in self.service.run_handles().values()
            for run_id, handle in bucket.items()
            if handle.token.cancelled
        }

    # 请求处理器。

    async def _report(self, handle: Any, req_id: str) -> None:
        """等待 RunOutcome，将终态翻译为 completed 或 error 帧。"""
        try:
            outcome = await handle.result()
        except asyncio.CancelledError:  # pragma: no cover - 关闭路径
            await self.writer.send_completed_frame(req_id, handle.run_id)
            raise

        if outcome.status in ("failed", "rejected"):
            await self.writer.send_error_frame(
                req_id, outcome.error_code or "AGENT_ERROR", (outcome.error_message or "agent failed")[:500]
            )

            return

        # ``finished`` / ``cancelled`` / ``rejected``：对 TUI 而言都是"这一轮结束了"。
        # ``rejected``（同会话 busy）刻意仍发 ``completed``——它没有产生任何事件，也不失败；
        await self.writer.send_completed_frame(req_id, handle.run_id)

    async def _handle_session_start(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.start``：启动一次运行并立即返回（**不** await 执行）。

        读循环必须保持可响应，这样 ``session.cancel`` 才能在生成途中被送达。
        """
        if req_id is None:
            await self.writer.send_error_frame(None, "BAD_REQUEST", "session.start requires an id")

            return

        requested = params.get("sessionCode")
        # 已登记的 code 原样使用；缺省走 -s 默认会话；未知则新建（见 SessionManager.resolve）。
        session_code = requested if isinstance(requested, str) and requested else await self._default_session()

        input_text = params.get("input", "")
        sink = RunEventSink(self.writer, req_id)
        options = params.get("executeKwargs") or {}
        if not isinstance(options, dict):
            await self.writer.send_error_frame(req_id, "BAD_REQUEST", "executeKwargs must be an object")
            return
        options = dict(options)
        resume = options.pop("resume", None)
        text = input_text if isinstance(input_text, str) else ""
        if resume is not None:
            handle = await self.service.resume(ResumeRequest(session_code, ResumeInterrupt(resume, text), req_id), sink)
        else:
            handle = await self.service.start(RunRequest(session_code, ChatInput(text), options, req_id), sink)
        # service.runs.start 只 create_task 不 await，任务尚未执行，此处绑定 runId 先于任何事件帧。
        # TUI 按 runId 归属 event 帧，空 runId 会被丢弃。
        sink.run_id = handle.run_id
        # 登记在 service 里之后才发 ``started``：调用方拿到 runId 时它一定可被取消。
        await self.writer.send_started_frame(req_id, handle.run_id)
        asyncio.create_task(self._report(handle, req_id), name=f"report-{handle.run_id}")

    def _session_of(self, run_id: str) -> str | None:
        """``run_id -> session_code``（经 service 的索引，不是本类的登记表）。"""
        for session_code, bucket in self.service.run_handles().items():
            handle = bucket.get(run_id)
            if handle is not None and not handle.settled:
                return session_code

        return None

    async def _handle_session_cancel(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.cancel``：转发给 service（两步取消的唯一实现）。

        ``runId`` 缺失或类型不对回 ``BAD_REQUEST``。未知或已结束的运行**上报**
        ``cancelled: False`` 而不是静默成功，这样父方不会误以为自己取消了某个仍在运行的东西。
        """
        run_id = params.get("runId")
        if not isinstance(run_id, str) or not run_id:
            await self.writer.send_error_frame(req_id, "BAD_REQUEST", "session.cancel requires params.runId")

            return

        session_code = self._session_of(run_id)
        cancelled = False
        if session_code is not None:
            outcome = await self.service.cancel(session_code, run_id)
            cancelled = outcome.cancelled

        payload = {"runId": run_id, "cancelled": cancelled}
        if not cancelled:
            payload["reason"] = "no active run"
        await self.writer.send_result_frame(req_id, payload)

    async def _handle_session_get(self, params: dict[str, Any], req_id: str | None) -> None:
        """``session.get``：无副作用地查一条会话记录（身份记账，不存消息）。"""
        session_code = params.get("sessionCode")
        payload: dict[str, Any] = {
            "session": _record(
                await self.service.session_info(session_code if isinstance(session_code, str) else None)
            ),
            "sessionCode": session_code,
        }

        await self.writer.send_result_frame(req_id, payload)

    async def _handle_agent_info(self, req_id: str | None) -> None:
        session_code = await self._default_session()
        """``agent.info``：``/`` 与 ``@`` 菜单所需的三个列表（实时平台拉取）。"""
        config = await self.service.describe_agent_config(session_code)

        await self.writer.send_result_frame(req_id, {"agentInfo": config})

    async def _handle_agent_llms(self, req_id: str | None) -> None:
        session_code = await self._default_session()
        """``agent.llms``：``/models`` 所需的模型列表。"""
        llms = await self.service.list_llms(session_code)

        await self.writer.send_result_frame(req_id, {"items": llms})

    async def _handle_config_info(self, req_id: str | None) -> None:
        session_code = await self._default_session()
        """``config.info``：四个只读配置浮层的启动快照（与 ``agent.info`` 的实时拉取分离）。"""
        snapshot = await self.service.describe_config_snapshot(session_code)

        await self.writer.send_result_frame(req_id, {"config": snapshot})

    async def _handle_config_set(self, params: dict[str, Any], req_id: str | None) -> None:
        session_code = await self._default_session()
        """``config.set``：写入一条声明式覆盖。对错误参数从不抛异常。"""
        kind = params.get("kind")
        value = params.get("value")
        kind = kind if isinstance(kind, str) else ""
        value = value if isinstance(value, str) else ""

        if kind not in ("models", "agents", "mcps", "knowledge") or not value:
            await self.writer.send_result_frame(req_id, {"ok": False})

            return

        await self.service.set_local_declaration(session_code, kind, value)

        await self.writer.send_result_frame(req_id, {"ok": True, "kind": kind, "value": value})

    async def _handle_auth_login(self, params: dict[str, Any], req_id: str | None) -> None:
        """``auth.login``：写入会话 token。**只进不出**——任何应答都不回显它。"""
        token = params.get("token")
        token = token if isinstance(token, str) else ""

        if not token:
            await self.writer.send_result_frame(req_id, {"ok": False, "hasToken": await self.service.has_token()})

            return

        # 交互式登录**落盘**（``persist=True`` 是默认值）——与 ACP 启动路径的
        # ``persist=False`` 相对。
        await self.service.login(token)
        await self.writer.send_result_frame(req_id, {"ok": True, "hasToken": True})

    async def _handle_auth_logout(self, req_id: str | None) -> None:
        """``auth.logout``：丢弃会话 token。token 纪律同 ``auth.login``。"""
        await self.service.logout()
        await self.writer.send_result_frame(req_id, {"ok": True, "hasToken": False})

    async def _handle_shutdown(self, req_id: str | None) -> None:
        """``process.shutdown``：先回 ``ok`` 再触发关闭回调（可以是协程）。"""
        await self.writer.send_result_frame(req_id, {"ok": True})
        if self._shutdown_cb is not None:
            result = self._shutdown_cb()
            if asyncio.iscoroutine(result):
                await result

    # 关闭运行时和输出。

    async def shutdown(self) -> None:
        """立即取消所有未结束的运行并关 service（含 owned saver），然后关闭 writer。

        取消与等待落定由 ``RuntimeService.close_all`` 统一完成（与 headless / acp 同一条路径）；
        writer 必须在运行落定之后才关，因为运行仍在向它写帧。
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        try:
            await self.service.close_all("agui shutdown", grace=0)
        finally:
            await self.writer.close()

    # 请求路由。

    async def _dispatch(self, method: str | None, params: dict[str, Any] | None, req_id: str | None) -> None:
        """按 ``method`` 路由到具体 handler。可能抛异常，由 :meth:`handle` 兜底。"""
        params = params if isinstance(params, dict) else {}

        if method == "session.start":
            await self._handle_session_start(params, req_id)
        elif method == "session.cancel":
            await self._handle_session_cancel(params, req_id)
        elif method == "session.get":
            await self._handle_session_get(params, req_id)
        elif method == "session.list":
            await self.writer.send_result_frame(req_id, {"items": await self.service.list_sessions()})
        elif method == "session.create":
            await self.writer.send_result_frame(req_id, (await self.service.new_session()).record())
        elif method == "agent.info":
            await self._handle_agent_info(req_id)
        elif method == "agent.llms":
            await self._handle_agent_llms(req_id)
        elif method == "config.info":
            await self._handle_config_info(req_id)
        elif method == "config.set":
            await self._handle_config_set(params, req_id)
        elif method == "auth.login":
            await self._handle_auth_login(params, req_id)
        elif method == "auth.logout":
            await self._handle_auth_logout(req_id)
        elif method == "process.shutdown":
            await self._handle_shutdown(req_id)
        else:
            await self.writer.send_error_frame(req_id, "UNKNOWN_METHOD", f"unknown method: {method!r}")

    async def handle(self, method: str | None, params: dict[str, Any] | None, req_id: str | None) -> None:
        """分发一个请求，捕获处理异常并发送错误帧。

        ``INTERNAL_ERROR`` 与 ``BAD_REQUEST``/``UNKNOWN_METHOD`` 区分开：后者是调用方可以纠正的
        协议误用，前者是本进程自身的处理失败——调用方看到 error 帧就够了，具体异常也记录到日志
        （且只记类型与消息，不记 ``params``，因为其中可能含有用户输入）。
        """
        try:
            await self._dispatch(method, params, req_id)
        except RuntimeFault as exc:
            await self.writer.send_error_frame(req_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 一条请求的失败绝不上抛到读循环
            logger.info(f"request handler failed for method={method!r}: {exc!r}")
            await self.writer.send_error_frame(req_id, "INTERNAL_ERROR", f"{type(exc).__name__}: {exc}")


async def _process_line(line: str, dispatcher: Dispatcher) -> None:
    """解析一行协议并路由，畸形输入返回 PARSE_ERROR。"""
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        req = None

    if not isinstance(req, dict):
        await dispatcher.writer.send_error_frame(None, "PARSE_ERROR", line[:120])
        return

    req_id = req.get("id")
    method = req.get("method")
    params = req.get("params")

    await dispatcher.handle(
        method if isinstance(method, str) else None,
        params,
        req_id if isinstance(req_id, str) else None,
    )


async def _mirror_stop_event(source: asyncio.Event, target: asyncio.Event) -> None:
    """将外部关闭信号汇入读循环使用的 stop 事件。"""
    await source.wait()
    target.set()


async def run_agui(service: Any) -> int:
    """通过公共行 I/O 运行 AGUI；所有退出路径都关闭运行时和 writer。"""
    shutdown_requested = async_shutdown_event()
    use_daemon_default_executor("headless-sdk")
    writer = FrameWriter(protocol_out())
    stop = asyncio.Event()
    dispatcher = Dispatcher(writer, service, shutdown_cb=stop.set)
    reader = LineReader(
        sys.stdin.buffer, eof_line=False, on_oversized=lambda: logger.info("agui: oversized frame dropped")
    )
    watchers: list[asyncio.Task[Any]] = []
    exit_reason = "process.shutdown requested"
    try:
        await writer.start()
        watchers.append(asyncio.create_task(_mirror_stop_event(shutdown_requested, stop), name="agui-shutdown"))
        watchers.append(asyncio.create_task(_mirror_stop_event(writer.stopped, stop), name="agui-output-stop"))
        if shutdown_requested.is_set():
            stop.set()
        if not await writer.send_ready_frame(sdk_file=sdk_file()):
            exit_reason = "startup write failed"
            return 0
        logger.info(f"SDK runtime: {sdk_file()}")
        while not stop.is_set():
            try:
                line = await until_stopped(reader.receive(), stop)
            except (OSError, ValueError) as exc:
                exit_reason = f"stdin read failed: {exc!r}"
                break
            if line is None:
                if writer.stopped.is_set():
                    exit_reason = "protocol output stopped"
                elif not stop.is_set():
                    exit_reason = "stdin EOF (TUI closed the command pipe)"
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                await _process_line(text, dispatcher)
    except Exception as exc:  # noqa: BLE001 - 输入或分发失败仍必须关闭运行时
        exit_reason = f"stdin loop crashed: {exc!r}"
    finally:
        reader.close()
        for task in watchers:
            task.cancel()
        await asyncio.gather(*watchers, return_exceptions=True)
        try:
            await dispatcher.shutdown()
        finally:
            if shutdown_requested.is_set():
                exit_reason = "shutdown signal received"
            logger.info(exit_reason)
    return 0


#: 嵌入式入口（``python -m aidev_agent_cli.adapters.agui``）用的资源预设。
EMBEDDED_RESOURCES = ("platform", "approval", "local", "mcp", "knowledge", "workspace", "runtime")


def run_embedded() -> int:
    """``python -m`` 嵌入式入口：**先**装启动钩子，**再**建 runtime 并进帧循环。

    顺序在这里是契约：``install_startup_hooks`` 完成日志 / guard / 环境 / OTel 之后，
    ``build_runtime`` 与 SDK 执行才被允许发生。
    """
    install_startup_hooks()

    return asyncio.run(run_agui(build_runtime("agui", resources=EMBEDDED_RESOURCES)))


if __name__ == "__main__":
    raise SystemExit(run_embedded())
