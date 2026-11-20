# -*- coding: utf-8 -*-
"""测试用路径真源。

子进程仍以 ``src/agent`` 为 cwd：``.env`` 加载、``AGENT_TUI_CWD`` 的相对路径、
``~/.bkai`` 都依赖这个语义。迁移到插件后 ``parents[N]`` 会指向 ``src/plugins``，
因此所有测试统一从这里取路径，不再各自推算。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: ``src/plugins/aidev_agent_cli``
PLUGIN_ROOT = Path(__file__).resolve().parents[2]
#: ``src/agent`` —— 子进程 cwd 语义不变
#: 它是 ``src/plugins`` 的兄弟目录，不能靠 ``parents[N]`` 直接取到。
_HERE = Path(__file__).resolve()
AGENT_ROOT = _HERE.parents[4] / "agent"
#: 当前解释器；``make test`` 下即 SDK venv 的 python
PYTHON = sys.executable


def plugin_pythonpath() -> str:
    """子进程用的 ``PYTHONPATH``：插件根 + SDK 根（未装 wheel 时两侧都要可解析）。

    单点真源：``aidev_agent_cli`` 与 ``aidev_agent`` 在 checkout 里是两个目录，子进程断言
    （无 SDK 副作用、sdk 根无 SDK、启动顺序）必须两边都能 import 才有意义。调用方不要
    各自拼路径。
    """
    parts = [str(PLUGIN_ROOT), str(AGENT_ROOT)]
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)

    return os.pathsep.join(parts)
