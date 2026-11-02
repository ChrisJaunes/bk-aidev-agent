# -*- coding: utf-8 -*-
"""沙箱侧桥客户端（prelude）源码生成。

生成的文件 ``_aidev_bridge.py`` 与 ``user_code.py`` / ``run.py`` 一起上传到沙箱的
独立执行目录。它只依赖 Python 标准库：

1. 提供 ``_aidev_call``：把请求原子写入 ``calls/req_<seq>.json``，轮询
   ``calls/resp_<seq>.json``，失败以 :class:`CodeModeToolError` 暴露给模型代码；
2. 为每个可调用工具生成同名同步函数（源码见 :mod:`.manifest`）；
3. 超过单段代码调用上限时抛 ``CodeModeBudgetExceeded``。

沙箱内不接触凭据：环境变量沿用各 runtime 现有的注入口径，prelude 不新增注入。
"""

from __future__ import annotations

from typing import Iterable

from .manifest import ToolManifestEntry, generate_stub_source

PRELUDE_FILENAME = "_aidev_bridge.py"
USER_CODE_FILENAME = "user_code.py"
RUNNER_FILENAME = "run.py"
CALLS_DIRNAME = "calls"
STATUS_FILENAME = "status.json"

_CLIENT_TEMPLATE = '''# -*- coding: utf-8 -*-
"""AIDev code mode bridge client (generated; do not edit)."""

import json
import os
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_CALLS_DIR = os.path.join(_HERE, "calls")
_POLL_INTERVAL = @@POLL_INTERVAL@@
_CALL_TIMEOUT = @@CALL_TIMEOUT@@
_MAX_TOOL_CALLS = @@MAX_TOOL_CALLS@@

_seq = 0
_tool_calls = 0
_ALLOWED_TOOLS = set()


class CodeModeToolError(Exception):
    """工具调用失败；模型代码可捕获后自行处理。"""


class CodeModeBudgetExceeded(Exception):
    """单段代码内的工具调用次数超限。"""


def _write_json_atomic(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _wait_response(seq):
    path = os.path.join(_CALLS_DIR, "resp_%04d.json" % seq)
    deadline = time.time() + _CALL_TIMEOUT
    while time.time() < deadline:
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)
            except (OSError, ValueError):
                time.sleep(_POLL_INTERVAL)
                continue
        time.sleep(_POLL_INTERVAL)
    raise CodeModeToolError("tool call %s timed out waiting for host response" % seq)


def _aidev_call(name, kwargs):
    global _seq, _tool_calls
    if name not in _ALLOWED_TOOLS:
        raise CodeModeToolError("unknown tool: %r (not in the code mode tool manifest)" % (name,))
    _tool_calls += 1
    if _tool_calls > _MAX_TOOL_CALLS:
        raise CodeModeBudgetExceeded(
            "tool call budget exceeded: at most %s calls per code block" % _MAX_TOOL_CALLS
        )
    _seq += 1
    seq = _seq
    os.makedirs(_CALLS_DIR, exist_ok=True)
    _write_json_atomic(
        os.path.join(_CALLS_DIR, "req_%04d.json" % seq),
        {"seq": seq, "tool": name, "args": kwargs},
    )
    response = _wait_response(seq)
    if not response.get("ok"):
        raise CodeModeToolError(response.get("error") or ("tool %s failed" % name))
    return response.get("content", "")


def tool_call_count():
    return _tool_calls
'''


def build_prelude_source(
    entries: Iterable[ToolManifestEntry],
    *,
    max_tool_calls: int,
    call_timeout: int,
    poll_interval: float,
) -> str:
    """生成 ``_aidev_bridge.py`` 的完整源码（桥客户端 + 工具桩）。"""

    entries = list(entries)
    client = (
        _CLIENT_TEMPLATE.replace("@@POLL_INTERVAL@@", repr(float(poll_interval)))
        .replace("@@CALL_TIMEOUT@@", str(int(call_timeout)))
        .replace("@@MAX_TOOL_CALLS@@", str(int(max_tool_calls)))
    )
    names = [entry.name for entry in entries]
    stubs = generate_stub_source(entries)
    tail = [
        "",
        f"TOOL_NAMES = {names!r}",
        "_ALLOWED_TOOLS.update(TOOL_NAMES)",
        "TOOL_FUNCTIONS = {name: globals()[name] for name in TOOL_NAMES}",
        "",
    ]
    return client + "\n" + stubs + "\n".join(tail)


__all__ = [
    "CALLS_DIRNAME",
    "PRELUDE_FILENAME",
    "RUNNER_FILENAME",
    "STATUS_FILENAME",
    "USER_CODE_FILENAME",
    "build_prelude_source",
]
