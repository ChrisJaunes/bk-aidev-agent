# -*- coding: utf-8 -*-
"""acp-dsh 子进程入口（测试用）：与 ``--protocol acp-dsh`` 相同，另注入一个记录调用的工具。"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from aidev_agent_cli.infrastructure.startup import install_startup_hooks

    install_startup_hooks()

    from aidev_agent_cli.adapters.acp_dsh import run_acp_dsh
    from aidev_agent_cli.runtime.service import build_runtime

    return run_acp_dsh(
        build_runtime("acp-dsh", resources=("injected",)),
        tool_factories=("tests.integration._acp_dsh_entry:build_fake_tool",),
    )


def build_fake_tool():
    from langchain_core.tools import tool

    record = os.environ.get("ACP_DSH_TOOL_RECORD", "")

    @tool
    def fake_tool(q: str) -> str:
        """记录调用并返回结果。"""
        if record:
            with open(record, "a", encoding="utf-8") as fh:
                fh.write(q + "\n")
        return f"tool-result:{q}"

    return fake_tool


if __name__ == "__main__":
    sys.exit(main())