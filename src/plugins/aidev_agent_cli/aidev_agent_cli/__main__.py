# -*- coding: utf-8 -*-
"""命令入口：参数解析 → 启动钩子 → adapter（协议）× 资源来源 → runtime。

参数
----

==================  ==========================================================
-m / --model        默认模型（会话请求显式给出的优先）
--resources         资源来源，逗号分隔，按顺序叠加（低 → 高优先级）；
                    预设 default，见 resources.sources
-s / --session      默认会话 code（请求不带会话时使用）
--protocol          交互协议：acp-dsh（默认）/ agui / tui（agui 别名）/ acp
--hooks             启用 hook：JSON 对象或 @文件（{"timeout_ms":..,"points":[..],"module":..}）；
                    无 module 时裁决经各协议回调客户端；带 module 时在 worker 内加载该
                    Python 模块（模块名或 .py 路径），按 SDK 6 个点位名取处理器；
                    省略时读环境变量 BKAI_AGENT_HOOKS，仍为空即关闭
-h / --help         帮助
==================  ==========================================================

三个协议共用同一个资源管理器（resources.manager.LocalResourceManager），任意协议都可与
任意资源组合搭配。省略 --resources 时取 default 预设。

参数错误一律「非零退出 + stderr 一句话」，发生在协议握手之前，**不进 stdout**
（协议 stdout 必须干净）。

启动顺序（D4）
--------------

1. 解析 argv（纯标准库，含 resources.sources）；
2. **装 stdout guard**——utils.stdio 只依赖标准库，这一步不导入任何 SDK；
3. 发布进程环境、装出网限制、加固 sys.path；
4. **此时才**导入 adapter：agui / acp 会连带导入 ag_ui / acp SDK。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import re
import sys

from .adapters.agui import run_agui
from .hooks.handler import parse_hooks_option
from .utils.errors import HeadlessError
from .utils.runs import LaunchOptions
from .runtime.settings import BkAiConfig
from .bootstrap.startup import install_startup_hooks
from .adapters.streams import ProtocolChannel
from .resources.sources import PLATFORM_SOURCES, SOURCES, parse_resources
from .runtime.process_config import ProcessConfig
from .runtime.service import build_runtime

#: --protocol 的取值。tui 是 agui 的别名。
PROTOCOLS = ("acp-dsh", "agui", "acp", "tui")

#: 与 ``--hooks`` 等价的环境变量（agent_tui 等不改启动参数的宿主用它启用 hook）。
HOOKS_ENV = "BKAI_AGENT_HOOKS"

#: 未安装 acp extra 时的提示（不是 ImportError 栈）。
ACP_EXTRA_MISSING = (
    'ACP 模式需要 agent-client-protocol：请安装 aidev-agent-cli[acp]（pip install "aidev-agent-cli[acp]"）。'
)


def normalize_protocol(protocol: str) -> str:
    """把 tui 归一为 agui；其余原样返回。"""
    return "agui" if protocol == "tui" else protocol


def _parser() -> argparse.ArgumentParser:
    presets = f"default = {','.join(PLATFORM_SOURCES)}"
    parser = argparse.ArgumentParser(prog="aidev_agent_cli", description="Run the aidev-agent CLI stdio runtime")
    parser.add_argument("-m", "--model", default="", help="default model for new sessions")
    parser.add_argument(
        "--resources",
        default=None,
        metavar="SOURCE[,SOURCE...]",
        help=(
            f"resource sources stacked in order (low -> high priority): {', '.join(SOURCES)}. "
            f"Presets: {presets}. Omitted: default"
        ),
    )
    parser.add_argument("-s", "--session", default="", help="default session code")
    parser.add_argument(
        "--protocol",
        choices=PROTOCOLS,
        default="acp-dsh",
        help=(
            "acp-dsh (default): ACP + _aidev/* extensions for dsh hosts; agui: versioned frames; "
            "tui: alias of agui; acp: Agent Client Protocol"
        ),
    )
    parser.add_argument(
        "--hooks",
        default=None,
        metavar="JSON|@FILE",
        help=(
            "enable hooks: JSON object or @file with timeout_ms / points / module; "
            "module loads a Python hook module in the worker, otherwise verdicts come from the protocol client. "
            "Falls back to $BKAI_AGENT_HOOKS"
        ),
    )

    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    protocol = normalize_protocol(args.protocol)

    try:
        resources = parse_resources(args.resources)
    except ValueError as exc:
        # 握手之前、stdout 之外：一条可读的说明 + 非零退出。
        print(f"--resources: {exc}", file=sys.stderr)

        return 2

    try:
        hooks = parse_hooks_option(args.hooks or os.environ.get(HOOKS_ENV) or None)
    except HeadlessError as exc:
        print(f"--hooks: {exc.message}", file=sys.stderr)

        return 2

    # 启动钩子先于任何**执行**：adapter 与 runtime 在模块顶层已导入（它们的导入期没有
    # 文件/环境副作用），但 SDK 执行、日志文件、环境发布、出网限制都由本函数按序发起。
    # 进程级本地配置只在这里解析一次：env 交给启动钩子，整份交给 build_runtime。
    bkai = BkAiConfig.read()
    install_startup_hooks(bkai.env)

    adapter = resolve_adapter(protocol)
    if adapter is None:
        print(ACP_EXTRA_MISSING, file=sys.stderr)

        return 2

    # 唯一的运行时组装点：所有协议同一条路径，身份只来自 bkai_tui.json。
    launch = LaunchOptions(model=args.model, session=args.session, resources=resources, workspace_root=os.getcwd())
    service = build_runtime(protocol, ProcessConfig(bkai=bkai, launch=launch), hooks=hooks)

    # 协议通道在 guard 安装之后取：输入是 stdin，输出是 guard 保留的 fd 1 副本。
    return adapter(service, ProtocolChannel.stdio())


def resolve_adapter(protocol: str):
    """按协议取 adapter 的运行入口（``(service, channel) -> int``）；缺 acp extra 时返回 None。

    ``agui`` 的 adapter 在模块顶部导入（无 SDK 导入期副作用）。``acp`` 是**可选 extra**，
    这里是本包**唯一**的 ACP 装载点，用 ``importlib.import_module`` 显式表达动态边界：
    缺 extra 时入口给出 ``ACP_EXTRA_MISSING`` 提示而不是导入栈。只有缺失模块名以 ``acp``
    开头才算"缺 extra"，其他导入失败原样抛出——不吞真实错误。
    """
    if protocol == "agui":
        return lambda service, channel: asyncio.run(run_agui(service, channel))

    try:
        adapter = importlib.import_module("aidev_agent_cli.adapters.acp")
    except ImportError as exc:
        name = exc.name or ""
        if name:
            missing_acp = name == "acp" or name.startswith("acp.")
        else:
            missing_acp = re.search(r"\bmodule named '?acp\b", str(exc)) is not None
        if not missing_acp:
            raise

        return None

    profile = adapter.PROFILES[protocol]
    return lambda service, channel: adapter.run_acp(service, channel, profile=profile)


if __name__ == "__main__":
    raise SystemExit(main())