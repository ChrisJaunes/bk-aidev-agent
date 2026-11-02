# -*- coding: utf-8 -*-
"""宿主侧桥执行器：把一个执行目录的往返协议跑完。

协议（见技术方案「沙箱内目录与协议」）::

    <run_dir>/_aidev_bridge.py   # prelude：桥客户端 + 工具桩
    <run_dir>/user_code.py       # 模型产出的代码
    <run_dir>/run.py             # 入口
    <run_dir>/calls/req_<seq>.json
    <run_dir>/calls/resp_<seq>.json
    <run_dir>/status.json

宿主只依赖 ``RuntimeBackend`` 的四个能力：``scratch_dir`` / ``ls_info`` /
``upload_files`` / ``download_files`` 与 ``execute``，四类 runtime 共用同一套实现。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any

from aidev_agent.config import settings
from aidev_agent.core.tools.runtime_tools.types import RuntimeBackend

from .errors import (
    STATUS_BUDGET_EXCEEDED,
    STATUS_INFRA_ERROR,
    STATUS_OK,
    STATUS_TIMEOUT,
    CodeModeUnsupported,
)
from .invoker import CodeModeInvoker
from .manifest import ToolManifestEntry
from .prelude import (
    CALLS_DIRNAME,
    PRELUDE_FILENAME,
    RUNNER_FILENAME,
    STATUS_FILENAME,
    USER_CODE_FILENAME,
    build_prelude_source,
)

logger = logging.getLogger(__name__)

_TRUNCATION_MARKER = "\n[Output was truncated due to size limits]"

_RUNNER_TEMPLATE = '''# -*- coding: utf-8 -*-
"""AIDev code mode runner (generated; do not edit)."""

import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import _aidev_bridge as bridge


def _write_status(status, error_type=None, message=""):
    payload = {
        "status": status,
        "tool_calls": bridge.tool_call_count(),
        "error_type": error_type,
        "message": message,
    }
    tmp = os.path.join(_HERE, "status.json.tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, os.path.join(_HERE, "status.json"))


def main():
    code_path = os.path.join(_HERE, "user_code.py")
    try:
        with open(code_path, encoding="utf-8") as handle:
            source = handle.read()
        compiled = compile(source, "user_code.py", "exec")
    except SyntaxError:
        traceback.print_exc()
        _write_status("syntax_error", "SyntaxError", "code failed to compile")
        return 2

    scope = {"__name__": "__main__", "__file__": code_path}
    scope.update(bridge.TOOL_FUNCTIONS)
    try:
        exec(compiled, scope)
    except bridge.CodeModeBudgetExceeded as exc:
        print("[code_mode] budget exceeded: %s" % exc, file=sys.stderr)
        _write_status("budget_exceeded", "CodeModeBudgetExceeded", str(exc))
        return 3
    except bridge.CodeModeToolError as exc:
        traceback.print_exc()
        _write_status("tool_error", "CodeModeToolError", str(exc))
        return 4
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
        status = "ok" if code == 0 else "runtime_error"
        _write_status(status, None if code == 0 else "SystemExit", str(exc.code or ""))
        return code
    except BaseException as exc:
        traceback.print_exc()
        _write_status("runtime_error", type(exc).__name__, str(exc))
        return 1

    _write_status("ok")
    return 0


sys.exit(main())
'''


@dataclass
class BridgeResult:
    """一次代码执行的归一化结果。"""

    status: str
    tool_calls: int = 0
    output: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK


def normalize_code(code: str) -> str:
    """剥离 ```python 围栏；其余内容原样保留。"""

    text = (code or "").strip()
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() in ("```", "'''"):
        lines = lines[:-1]
    return "\n".join(lines)


def resolve_backend(resolver: Any) -> RuntimeBackend:
    """确定 code mode 的执行后端（REQ-9）。

    取 ``resolver.default_runtime``；该名字没有实例但只注册了一个运行时则回退到它；
    都无法确定时抛 :class:`CodeModeUnsupported`，不伪造本地执行。
    """

    backends = getattr(resolver, "_backends", None) or {}
    default_runtime = getattr(resolver, "default_runtime", "") or ""
    if default_runtime in backends:
        return backends[default_runtime]
    if len(backends) == 1:
        return next(iter(backends.values()))
    available = ", ".join(sorted(backends)) or "(none)"
    raise CodeModeUnsupported(
        f"code mode 无法确定默认运行时（default_runtime={default_runtime!r}，已注册运行时: {available}）；"
        "请显式配置唯一的默认 runtime"
    )


def ensure_code_mode_capable(backend: RuntimeBackend) -> None:
    """校验后端具备 code mode 依赖的能力；缺失时抛 ``CodeModeUnsupported``。

    以「子类是否重写基类方法」判定能力存在，避免基类的 ``NotImplementedError``
    声明被误当成可用实现。
    """

    required = ("ls_info", "upload_files", "download_files", "execute", "scratch_dir")
    missing = []
    for name in required:
        implementation = getattr(type(backend), name, None)
        base = getattr(RuntimeBackend, name, None)
        if not callable(implementation) or implementation is base:
            missing.append(name)
    if missing:
        raise CodeModeUnsupported(f"运行时后端 {type(backend).__name__} 缺少 code mode 能力: {missing}")


class CodeModeBridge:
    """执行一次「上传 → 执行 → 轮询应答 → 汇总」的完整往返。"""

    def __init__(
        self,
        *,
        backend: RuntimeBackend,
        invoker: CodeModeInvoker,
        manifest: list[ToolManifestEntry],
        max_tool_calls: int | None = None,
        execute_timeout: int | None = None,
        output_limit: int | None = None,
        poll_interval: float | None = None,
    ) -> None:
        ensure_code_mode_capable(backend)
        self._backend = backend
        self._invoker = invoker
        self._manifest = manifest
        self._max_tool_calls = int(settings.CODE_MODE_MAX_TOOL_CALLS if max_tool_calls is None else max_tool_calls)
        self._execute_timeout = int(settings.CODE_MODE_EXECUTE_TIMEOUT if execute_timeout is None else execute_timeout)
        self._output_limit = int(settings.CODE_MODE_OUTPUT_LIMIT if output_limit is None else output_limit)
        self._poll_interval = float(settings.CODE_MODE_BRIDGE_POLL_INTERVAL if poll_interval is None else poll_interval)

    # ------------------------------------------------------------------
    # 对外入口
    # ------------------------------------------------------------------
    async def arun(
        self,
        code: str,
        *,
        run_id: str | None = None,
        config: dict | None = None,
        state: dict | None = None,
    ) -> BridgeResult:
        run_id = run_id or uuid.uuid4().hex[:12]
        try:
            run_dir = str(self._backend.scratch_dir(run_id))
        except Exception as exc:  # noqa: BLE001
            return BridgeResult(STATUS_INFRA_ERROR, error=f"scratch_dir 失败: {exc}")

        uploaded = await asyncio.to_thread(self._upload_program, run_dir, code)
        if uploaded is not None:
            return BridgeResult(STATUS_INFRA_ERROR, error=uploaded)

        command = f"python3 {run_dir}/{RUNNER_FILENAME}"
        exec_task = asyncio.create_task(
            asyncio.to_thread(
                self._backend.execute,
                command,
                self._execute_timeout,
                self._output_limit,
                config=config,
                state=state,
            )
        )
        tool_calls = 0
        seen: set[int] = set()
        try:
            while True:
                answered = await self._drain_requests(run_dir, seen, run_id=run_id, config=config, state=state)
                tool_calls += answered
                if exec_task.done():
                    break
                await asyncio.sleep(self._poll_interval)
            # 进程正常退出前不会遗留未应答请求；再排空一次以收拢退出竞态。
            tool_calls += await self._drain_requests(run_dir, seen, run_id=run_id, config=config, state=state)
            execute_result = exec_task.result()
        except Exception as exc:  # noqa: BLE001
            exec_task.cancel()
            return BridgeResult(STATUS_INFRA_ERROR, tool_calls=tool_calls, error=f"执行桥异常: {exc}")

        return self._compose_result(run_dir, execute_result, tool_calls)

    # ------------------------------------------------------------------
    # 内部步骤
    # ------------------------------------------------------------------
    def _upload_program(self, run_dir: str, code: str) -> str | None:
        prelude = build_prelude_source(
            self._manifest,
            max_tool_calls=self._max_tool_calls,
            call_timeout=self._execute_timeout,
            poll_interval=0.01,
        )
        files = [
            (f"{run_dir}/{PRELUDE_FILENAME}", prelude.encode("utf-8")),
            (f"{run_dir}/{USER_CODE_FILENAME}", normalize_code(code).encode("utf-8")),
            (f"{run_dir}/{RUNNER_FILENAME}", _RUNNER_TEMPLATE.encode("utf-8")),
        ]
        try:
            responses = self._backend.upload_files(files)
        except Exception as exc:  # noqa: BLE001
            return f"上传执行文件失败: {exc}"
        for response in responses:
            if response.get("error"):
                return f"上传执行文件失败: {response.get('path')}: {response.get('error')}"
        return None

    def _list_requests(self, run_dir: str) -> list[tuple[int, str]]:
        calls_dir = f"{run_dir}/{CALLS_DIRNAME}"
        try:
            infos = self._backend.ls_info(calls_dir)
        except Exception:  # noqa: BLE001
            return []
        requests: list[tuple[int, str]] = []
        for info in infos or []:
            path = str(info.get("path") or "")
            name = os.path.basename(path)
            if not name.startswith("req_") or not name.endswith(".json"):
                continue
            try:
                seq = int(name[len("req_") : -len(".json")])
            except ValueError:
                continue
            requests.append((seq, path))
        requests.sort(key=lambda item: item[0])
        return requests

    def _read_json(self, path: str) -> dict | None:
        try:
            responses = self._backend.download_files([path])
        except Exception:  # noqa: BLE001
            return None
        for response in responses or []:
            content = response.get("content")
            if content is None:
                continue
            try:
                return json.loads(content.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return None
        return None

    def _write_response(self, run_dir: str, seq: int, payload: dict) -> None:
        path = f"{run_dir}/{CALLS_DIRNAME}/resp_{seq:04d}.json"
        try:
            self._backend.upload_files([(path, json.dumps(payload, ensure_ascii=False).encode("utf-8"))])
        except Exception as exc:  # noqa: BLE001
            logger.warning("[code-mode] 写入内层调用响应失败 seq=%s: %s", seq, exc)

    async def _drain_requests(
        self,
        run_dir: str,
        seen: set[int],
        *,
        run_id: str,
        config: dict | None,
        state: dict | None,
    ) -> int:
        answered = 0
        for seq, path in self._list_requests(run_dir):
            if seq in seen:
                continue
            request = await asyncio.to_thread(self._read_json, path)
            if request is None:
                continue  # 半截文件：下一轮再读
            seen.add(seq)
            answered += 1
            payload = await self._handle_request(request, seq=seq, run_id=run_id, config=config, state=state)
            await asyncio.to_thread(self._write_response, run_dir, seq, payload)
        return answered

    async def _handle_request(
        self,
        request: dict,
        *,
        seq: int,
        run_id: str,
        config: dict | None,
        state: dict | None,
    ) -> dict:
        tool_name = str(request.get("tool") or "")
        args = request.get("args") or {}
        if seq > self._max_tool_calls:
            return {
                "seq": seq,
                "ok": False,
                "error": f"工具调用次数超过上限 {self._max_tool_calls}",
                "error_type": STATUS_BUDGET_EXCEEDED,
            }
        content, error = await self._invoker.ainvoke(
            tool_name,
            args,
            call_id=f"cm-{run_id}-{seq:04d}",
            config=config,
            state=state,
        )
        if error is not None:
            return {"seq": seq, "ok": False, "error": error, "error_type": "tool_error"}
        return {"seq": seq, "ok": True, "content": content}

    def _read_status(self, run_dir: str) -> dict | None:
        return self._read_json(f"{run_dir}/{STATUS_FILENAME}")

    def _compose_result(self, run_dir: str, execute_result: Any, tool_calls: int) -> BridgeResult:
        output = getattr(execute_result, "output", "") or ""
        exit_code = getattr(execute_result, "exit_code", None)
        truncated = bool(getattr(execute_result, "truncated", False))
        status_doc = self._read_status(run_dir)

        if status_doc is None:
            lowered = output.lower()
            if exit_code is None and ("timed out" in lowered or "timeout" in lowered):
                return BridgeResult(
                    STATUS_TIMEOUT,
                    tool_calls,
                    f"[code_mode] status={STATUS_TIMEOUT} tool_calls={tool_calls}\n{self._truncate(output)}",
                    "代码执行超时",
                )
            return BridgeResult(
                STATUS_INFRA_ERROR,
                tool_calls,
                f"[code_mode] status={STATUS_INFRA_ERROR} tool_calls={tool_calls}\n{self._truncate(output)}",
                "沙箱未产出 status.json（执行入口可能不可用）",
            )

        status = str(status_doc.get("status") or STATUS_INFRA_ERROR)
        tool_calls = int(status_doc.get("tool_calls") or tool_calls)
        body = self._truncate(output, truncated)
        message = str(status_doc.get("message") or "")
        if status == STATUS_OK:
            return BridgeResult(status, tool_calls, body, "")
        error_text = body if body.strip() else message
        header = f"[code_mode] status={status} tool_calls={tool_calls}"
        return BridgeResult(status, tool_calls, f"{header}\n{error_text}".rstrip(), message or error_text)

    def _truncate(self, text: str, truncated: bool = False) -> str:
        if len(text) > self._output_limit:
            return text[: self._output_limit] + _TRUNCATION_MARKER
        if truncated:
            return text + _TRUNCATION_MARKER
        return text


__all__ = [
    "BridgeResult",
    "CodeModeBridge",
    "ensure_code_mode_capable",
    "normalize_code",
    "resolve_backend",
]
