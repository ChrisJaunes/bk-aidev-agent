# -*- coding: utf-8 -*-
"""code mode（PTC）：模型用一段代码编排工具调用。

模块划分：

- :mod:`.manifest`：精简工具清单与代码桩生成；
- :mod:`.prelude`：沙箱侧桥客户端源码；
- :mod:`.bridge`：宿主侧「上传 → 执行 → 轮询应答 → 汇总」；
- :mod:`.invoker`：内层调用复用既有 ToolNode 执行链；
- :mod:`.tool`：``code_execute`` 工具；
- :mod:`.session` / :mod:`.errors`：请求级执行序号、重试与异常。
"""

from .bridge import BridgeResult, CodeModeBridge, ensure_code_mode_capable, resolve_backend
from .errors import (
    FAILURE_STATUSES,
    STATUS_BUDGET_EXCEEDED,
    STATUS_INFRA_ERROR,
    STATUS_OK,
    STATUS_RUNTIME_ERROR,
    STATUS_SYNTAX_ERROR,
    STATUS_TIMEOUT,
    STATUS_TOOL_ERROR,
    CodeModeRetryExhausted,
    CodeModeUnsupported,
)
from .invoker import CodeModeInvoker
from .manifest import ToolManifestEntry, build_manifest, generate_stub_source, render_manifest_prompt
from .session import CodeModeSession
from .tool import CODE_EXECUTE_TOOL_NAME, build_code_execute_tool

__all__ = [
    "CODE_EXECUTE_TOOL_NAME",
    "FAILURE_STATUSES",
    "STATUS_BUDGET_EXCEEDED",
    "STATUS_INFRA_ERROR",
    "STATUS_OK",
    "STATUS_RUNTIME_ERROR",
    "STATUS_SYNTAX_ERROR",
    "STATUS_TIMEOUT",
    "STATUS_TOOL_ERROR",
    "BridgeResult",
    "CodeModeBridge",
    "CodeModeInvoker",
    "CodeModeRetryExhausted",
    "CodeModeSession",
    "CodeModeUnsupported",
    "ToolManifestEntry",
    "build_code_execute_tool",
    "build_manifest",
    "ensure_code_mode_capable",
    "generate_stub_source",
    "render_manifest_prompt",
    "resolve_backend",
]
