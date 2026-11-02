# -*- coding: utf-8 -*-
"""code mode（PTC）的异常与状态定义。

沙箱内（``_aidev_bridge``）与宿主侧各自持有同名异常：沙箱内的异常用于让模型
代码 ``except`` 捕获，宿主侧的异常用于装配与重试终止。两者互不导入。
"""

from __future__ import annotations

from langgraph.errors import GraphBubbleUp

#: 执行状态集合；前七类都会以状态行回传模型。
STATUS_OK = "ok"
STATUS_SYNTAX_ERROR = "syntax_error"
STATUS_RUNTIME_ERROR = "runtime_error"
STATUS_TOOL_ERROR = "tool_error"
STATUS_TIMEOUT = "timeout"
STATUS_BUDGET_EXCEEDED = "budget_exceeded"
STATUS_INFRA_ERROR = "infra_error"

FAILURE_STATUSES = frozenset(
    {
        STATUS_SYNTAX_ERROR,
        STATUS_RUNTIME_ERROR,
        STATUS_TOOL_ERROR,
        STATUS_TIMEOUT,
        STATUS_BUDGET_EXCEEDED,
        STATUS_INFRA_ERROR,
    }
)


class CodeModeError(Exception):
    """code mode 宿主侧异常基类。"""


class CodeModeUnsupported(CodeModeError):
    """运行时后端缺少 code mode 必需能力，或无法确定唯一的执行后端。

    按 REQ-9 不降级到 local：宁可显式失败，也不在非预期环境下执行模型代码。
    """


class CodeModeRetryExhausted(GraphBubbleUp):
    """连续失败达到重试上限。

    继承 ``GraphBubbleUp``：``ToolNode`` 的默认错误处理器会原样重抛，图运行失败，
    AG-UI 输出 RUN_ERROR，不会把失败静默转成空结果的 ToolMessage（REQ-8）。
    """


__all__ = [
    "FAILURE_STATUSES",
    "STATUS_BUDGET_EXCEEDED",
    "STATUS_INFRA_ERROR",
    "STATUS_OK",
    "STATUS_RUNTIME_ERROR",
    "STATUS_SYNTAX_ERROR",
    "STATUS_TIMEOUT",
    "STATUS_TOOL_ERROR",
    "CodeModeError",
    "CodeModeRetryExhausted",
    "CodeModeUnsupported",
]
