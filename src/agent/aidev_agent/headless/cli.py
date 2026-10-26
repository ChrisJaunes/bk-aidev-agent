# -*- coding: utf-8 -*-
"""headless stdio 入口：stdin 逐行请求 → stdout 逐行事件 + 一条响应（REQ-6 / REQ-10）。

进程模型（方案 D1）：单进程多会话。``turn`` / ``resume`` 在独立工作线程执行，
使主线程可以继续读取 ``cancel`` / ``close``；同会话由会话锁串行，跨会话并发。
任一请求失败只影响该请求——异常一律转成响应，进程保持可用（ACC-13）。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from typing import Any, TextIO

from .errors import (
    AGENT_INTERNAL_ERROR,
    PROTOCOL_INVALID_PARAMS,
    HeadlessError,
)
from .events import build_error_event
from .hooks import HookConfig, HookPendingRouter
from .protocol import (
    METHOD_CANCEL,
    METHOD_CLOSE,
    METHOD_CREATE,
    METHOD_RESUME,
    METHOD_TURN,
    Request,
    encode_error,
    encode_event,
    encode_result,
    is_hook_reply,
    parse_request,
    require_param,
)
from .session import HeadlessSession, SessionConfig, SessionRegistry

logger = logging.getLogger("aidev-agent.headless")


# ============================== 日志与前置校验 ==============================


def configure_logging() -> None:
    """把 root logger 与全部 handler 指向 stderr，保证 stdout 只承载协议数据。"""
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
            f"aidev_agent.headless 需要内存消息处理器（InMemoryQueueMessageHandler），"
            f"当前为 {type(handler).__name__}；请设置 MESSAGE_HANDLER_TYPE=inmemory"
        )


# ============================== 分发 ==============================


class Dispatcher:
    """NDJSON 请求分发器。"""

    def __init__(self, *, stream: TextIO, registry: SessionRegistry | None = None):
        self._stream = stream
        self._write_lock = threading.Lock()
        self.hook_router = HookPendingRouter()
        self.registry = registry or SessionRegistry(
            stream=stream,
            write_lock=self._write_lock,
            hook_router=self.hook_router,
            write_line=self._write,
        )
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

    # ---------------- 行入口 ----------------

    def handle_line(self, line: str) -> None:
        payload = _try_json(line)
        if is_hook_reply(payload):
            # 答复行：投递给等待方，不产生响应行、不进请求解析、不报未知方法（REQ-18）
            self.hook_router.deliver(payload)
            return
        try:
            request = parse_request(line)
        except HeadlessError as exc:
            self._emit_error_line(_lenient_request_id(line), exc)
            return
        self.handle_request(request)

    def handle_request(self, request: Request) -> None:
        """处理一条已解析的请求（进程内调用可携带非 JSON 参数，如 ``event_sink``）。"""
        if request.method in (METHOD_TURN, METHOD_RESUME):
            worker = threading.Thread(
                target=self._run_session_task,
                args=(request,),
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
        except HeadlessError as exc:
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

    def _run_session_task(self, request: Request) -> None:
        try:
            session = self.registry.get(require_param(request.params, "session_code"))
            if request.method == METHOD_TURN:
                result = session.turn(
                    request_id=request.id,
                    input_text=_optional_str(request.params, "input") or "",
                    messages=_optional_messages(request.params),
                )
            else:
                result = session.resume(
                    request_id=request.id,
                    input_text=_optional_str(request.params, "input") or "",
                    resume_payload=request.params.get("resume"),
                )
        except HeadlessError as exc:
            self._respond_error(request.id, exc)
            return
        except Exception as exc:  # pylint: disable=broad-except
            logger.exception("headless %s failed: id=%s", request.method, request.id)
            self._respond_error(
                request.id,
                HeadlessError(AGENT_INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"),
            )
            return
        # 轮内已写出 RUN_ERROR 的失败轮：响应同样是 ok=false，错误码取自轮结果
        if result.get("status") == "error" and result.get("error"):
            self._write_error_payload(request.id, result["error"])
            return
        self._write(encode_result(request.id, result))

    def _create(self, request: Request) -> dict:
        params = request.params
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

        config = SessionConfig(
            agent_code=agent_code,
            model=_optional_str(llm, "model") or "",
            base_url=_optional_str(llm, "base_url"),
            auth_headers=llm.get("auth_headers") if isinstance(llm.get("auth_headers"), dict) else None,
            temperature=llm.get("temperature"),
            max_tokens=llm.get("max_tokens"),
            username=_optional_str(params, "username"),
            checkpointer=params.get("checkpointer"),
            event_sink=params.get("event_sink"),
            hooks=_optional_hooks(params),
            tools=_optional_tools(params),
        )
        if config.base_url:
            from aidev_agent.config import settings

            settings.set("LLM_GW_ENDPOINT", config.base_url)

        session = self.registry.create(session_code=session_code, config=config)
        try:
            session.prepare()
        except HeadlessError:
            self.registry.remove(session_code)
            raise
        except Exception as exc:  # pylint: disable=broad-except
            self.registry.remove(session_code)
            from .errors import AGENT_BUILD_FAILED

            raise HeadlessError(AGENT_BUILD_FAILED, f"{type(exc).__name__}: {exc}") from exc

        return {"session_code": session_code, "created": True}

    def _cancel(self, request: Request) -> dict:
        session = self._session_for(request)
        return session.cancel()

    def _close(self, request: Request) -> dict:
        session = self._session_for(request)
        return session.close()

    def _session_for(self, request: Request) -> HeadlessSession:
        session = self.registry.get(require_param(request.params, "session_code"))
        session.ensure_open()
        return session

    # ---------------- 写出 ----------------

    def _write(self, line: str) -> None:
        with self._write_lock:
            self._stream.write(line + "\n")
            self._stream.flush()

    def _respond_error(self, request_id: str, error: HeadlessError) -> None:
        """先写一条 RUN_ERROR 事件，再写结构化错误响应。"""
        self._write(encode_event(request_id, build_error_event(error.message)))
        self._write(encode_error(request_id, error))

    def _write_error_payload(self, request_id: str, payload: dict) -> None:
        error = HeadlessError(
            payload.get("code") or AGENT_INTERNAL_ERROR,
            payload.get("message") or "执行失败",
            payload.get("detail"),
        )
        self._write(encode_error(request_id, error))

    def _emit_error_line(self, request_id: str, error: HeadlessError) -> None:
        """协议层错误：仍产出事件行 + 响应行各一条（拿不到 id 时用空串）。"""
        self._write(encode_event(request_id, build_error_event(error.message)))
        self._write(encode_error(request_id, error))


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


def _optional_hooks(params: dict[str, Any]) -> HookConfig | None:
    """解析 ``create.params.hooks``；不传即完全关闭 hook 层（REQ-19）。"""
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


# ============================== 入口 ==============================


def serve(stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """从 ``stdin`` 读取 NDJSON 请求，向 ``stdout`` 写出事件与响应；EOF 时返回 0。"""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    dispatcher = Dispatcher(stream=stdout)
    for raw_line in stdin:
        line = raw_line.strip()
        if not line:
            continue
        dispatcher.handle_line(line)
    dispatcher.join()
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    assert_in_memory_message_handler()
    return serve()
