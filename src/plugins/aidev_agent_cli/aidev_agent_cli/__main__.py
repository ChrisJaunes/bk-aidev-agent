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
-h / --help         帮助
==================  ==========================================================

三个协议共用同一个资源管理器（resources.manager.LocalResourceManager），任意协议都可与
任意资源组合搭配。省略 --resources 时取 default 预设。

参数错误一律「非零退出 + stderr 一句话」，发生在协议握手之前，**不进 stdout**
（协议 stdout 必须干净）。

启动顺序（D4）
--------------

1. 解析 argv（纯标准库，含 resources.sources）；
2. **装 stdout guard**——infrastructure.stdio 只依赖标准库，这一步不导入任何 SDK；
3. 发布进程环境、装出网限制、加固 sys.path；
4. **此时才**导入 adapter：agui / acp 会连带导入 ag_ui / acp SDK。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import re
import sys

from .adapters.agui import run_agui
from .infrastructure.startup import install_startup_hooks
from .resources.sources import PLATFORM_SOURCES, SOURCES, parse_resources
from .runtime.service import build_runtime

#: --protocol 的取值。tui 是 agui 的别名。
PROTOCOLS = ("acp-dsh", "agui", "acp", "tui")

#: 未安装 acp extra 时的提示（不是 ImportError 栈）。
ACP_EXTRA_MISSING = (
    'ACP 模式需要 agent-client-protocol：请安装 aidev-agent-cli[acp]（pip install "aidev-agent-cli[acp]"）。'
)


def normalize_protocol(protocol: str) -> str:
    """把 tui 归一为 agui；其余原样返回。"""
    return "agui" if protocol == "tui" else protocol


def resolve_resources(resources: str | None) -> tuple[str, ...]:
    """展开 --resources 为来源序列；省略时取 ``default`` 预设。非法取值抛 ValueError。

    三个协议省略时都拿到同一个 ``default`` 预设。预设展开、去重、校验都由 :func:`parse_resources` 完成，这里返回的是
    已经解析完的序列，下游不再做任何默认值或预设展开。
    """
    names = ("default",) if resources is None else tuple(raw.strip() for raw in resources.split(",") if raw.strip())

    return parse_resources(names)


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

    return parser


def main(argv=None) -> int:
    args = _parser().parse_args(argv)
    protocol = normalize_protocol(args.protocol)

    try:
        resources = resolve_resources(args.resources)
    except ValueError as exc:
        # 握手之前、stdout 之外：一条可读的说明 + 非零退出。
        print(f"--resources: {exc}", file=sys.stderr)

        return 2

    # 启动钩子先于任何**执行**：adapter 与 runtime 在模块顶层已导入（它们的导入期没有
    # 文件/环境副作用），但 SDK 执行、日志文件、环境发布、出网限制都由本函数按序发起。
    install_startup_hooks()

    adapter = _import_adapter(protocol)
    if adapter is None:
        print(ACP_EXTRA_MISSING, file=sys.stderr)

        return 2

    # 唯一的运行时组装点：进程配置 / 身份 / checkpoint 存储 / 协议默认值都在这里决定。
    service = build_runtime(protocol, model=args.model, session=args.session, resources=resources)

    return adapter(service)


def _import_adapter(protocol: str):
    """按协议取 adapter 的运行入口（``(service) -> int``）；缺 acp extra 时返回 None。

    ``agui`` 的 adapter 在模块顶部导入（无 SDK 导入期副作用）；``acp`` 是**可选 extra**，
    因此在模块加载期经 :func:`_load_optional_acp_adapters` 一次性探测：缺 extra 时结果是
    ``None``，入口给出一行可读提示而不是导入栈。
    """
    if protocol == "agui":
        return lambda service: asyncio.run(run_agui(service))

    acp_adapters = _ACP_ADAPTERS
    if acp_adapters is None:
        return None

    return acp_adapters[0] if protocol == "acp-dsh" else acp_adapters[1]


def _load_optional_acp_adapters():
    """``(run_acp_dsh, run_acp)``；acp extra 未安装时返回 ``None``。

    这是本包**唯一**的 ACP 可选依赖装载点，用 ``importlib.import_module`` 显式表达动态边界
    （不是把 import 藏进函数体）：``acp`` 未安装时入口必须仍然可导入，并给出
    ``ACP_EXTRA_MISSING`` 提示。只有 ``ImportError.name`` 以 ``acp`` 开头才算"缺 extra"，
    其他导入失败原样抛出——不吞真实错误。
    """
    try:
        acp_dsh = importlib.import_module("aidev_agent_cli.adapters.acp_dsh")
        acp = importlib.import_module("aidev_agent_cli.adapters.acp")
    except ImportError as exc:
        name = exc.name or ""
        if name:
            missing_acp = name == "acp" or name.startswith("acp.")
        else:
            missing_acp = re.search(r"\bmodule named '?acp\b", str(exc)) is not None
        if not missing_acp:
            raise

        return None

    return acp_dsh.run_acp_dsh, acp.run_acp


#: 可选 extra 的导入结果：``(run_acp_dsh, run_acp)`` 或 ``None``（acp 未安装）。
_ACP_ADAPTERS = _load_optional_acp_adapters()


if __name__ == "__main__":
    raise SystemExit(main())
