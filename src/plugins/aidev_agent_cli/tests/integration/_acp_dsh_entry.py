# -*- coding: utf-8 -*-
"""acp-dsh 子进程入口（测试用）：与 ``--protocol acp-dsh`` 相同，另注入一个记录调用的工具。"""

from __future__ import annotations

import os
import sys


def main() -> int:
    from aidev_agent_cli.utils.runs import LaunchOptions
    from aidev_agent_cli.runtime.process_config import ProcessConfig
    from aidev_agent_cli.runtime.settings import BkAiConfig
    from aidev_agent_cli.bootstrap.startup import install_startup_hooks

    bkai = BkAiConfig.read()
    install_startup_hooks(bkai.env)

    if os.environ.get("ACP_DSH_PREWARM") == "1":
        from tests.integration import _prewarm

        _prewarm.install()

    from aidev_agent_cli.adapters.acp import ACP_DSH, run_acp
    from aidev_agent_cli.hooks.handler import parse_hooks_option
    from aidev_agent_cli.adapters.streams import ProtocolChannel
    from aidev_agent_cli.runtime.service import build_runtime

    return run_acp(
        build_runtime("acp-dsh", ProcessConfig(bkai=bkai, launch=LaunchOptions(resources=("injected",))), hooks=parse_hooks_option(os.environ.get("ACP_DSH_HOOKS"))
        ),
        ProtocolChannel.stdio(),
        profile=ACP_DSH,
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