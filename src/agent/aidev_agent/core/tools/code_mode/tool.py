# -*- coding: utf-8 -*-
"""``code_execute`` 工具：code mode 下模型唯一绑定的工具。

入参只有 ``code``；返回执行输出。失败时首行是
``[code_mode] status=<status> tool_calls=<n>``，其后是截断后的错误输出。
工具本身不带 ``metadata.approval``，不会触发人工中断（REQ-3）。
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.prebuilt import InjectedState

from .bridge import CodeModeBridge, resolve_backend
from .errors import STATUS_OK, CodeModeRetryExhausted
from .invoker import CodeModeInvoker
from .manifest import ToolManifestEntry
from .session import CodeModeSession

logger = logging.getLogger(__name__)

CODE_EXECUTE_TOOL_NAME = "code_execute"

CODE_EXECUTE_TOOL_DESCRIPTION = """在沙箱中执行一段 Python 代码来编排工具调用。

代码环境内已注入可调用工具的同步函数（函数名 = 工具名），直接调用即可；用 print 输出结果。
代码会完整执行一次（支持循环、条件分支与结果聚合），控制台输出回传给模型。
代码报错、工具异常或执行超时都会连同错误信息回传，可修正后重试。"""


def _empty_output() -> str:
    return "[code_mode] status=ok tool_calls=0 (no output)"


def build_code_execute_tool(
    *,
    session: CodeModeSession,
    resolver: Any,
    invoker: CodeModeInvoker,
    manifest: list[ToolManifestEntry],
    max_tool_calls: int | None = None,
    execute_timeout: int | None = None,
    output_limit: int | None = None,
    poll_interval: float | None = None,
) -> BaseTool:
    """构造 ``code_execute``；后端在首次执行时解析并复用。"""

    cached: dict[str, Any] = {}

    def _bridge() -> CodeModeBridge:
        bridge = cached.get("bridge")
        if bridge is None:
            backend = resolve_backend(resolver)
            bridge = CodeModeBridge(
                backend=backend,
                invoker=invoker,
                manifest=manifest,
                max_tool_calls=max_tool_calls,
                execute_timeout=execute_timeout,
                output_limit=output_limit,
                poll_interval=poll_interval,
            )
            cached["bridge"] = bridge
        return bridge

    async def code_execute(
        code: str,
        config: RunnableConfig,
        state: Annotated[dict, InjectedState] = None,
    ) -> str:
        """在沙箱中执行编排代码并返回执行输出。"""

        if not (code or "").strip():
            return "[code_mode] status=syntax_error tool_calls=0\n代码为空"

        bridge = _bridge()

        # 同一会话内的代码执行串行（不同会话各自持有独立 session/lock）。
        async with session.lock:
            result = await bridge.arun(code, run_id=session.next_run_id(), config=config, state=state)
        if result.status == STATUS_OK:
            session.register_success()
            return result.output or _empty_output()

        if session.register_failure():
            raise CodeModeRetryExhausted(
                f"code mode 连续失败达到重试上限（{session.max_retries} 次重试），本轮终止；"
                f"最后一次状态: {result.status}"
            )
        return result.output

    return StructuredTool.from_function(
        name=CODE_EXECUTE_TOOL_NAME,
        description=CODE_EXECUTE_TOOL_DESCRIPTION,
        func=None,
        coroutine=code_execute,
    )


__all__ = [
    "CODE_EXECUTE_TOOL_DESCRIPTION",
    "CODE_EXECUTE_TOOL_NAME",
    "build_code_execute_tool",
]
