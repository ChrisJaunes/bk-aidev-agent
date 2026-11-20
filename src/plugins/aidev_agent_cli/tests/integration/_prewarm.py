# -*- coding: utf-8 -*-
"""测试专用 worker 启动方式：预装载 SDK 的 forkserver 代替每轮 cold spawn。

生产 worker 每轮 ``spawn`` 新解释器并现场 import 整个 SDK（约 2.5s/轮）。集成用例关心协议、
hook、中断与生命周期语义，不是 SDK 冷启动；这里把 ``runtime.worker`` 看到的
``multiprocessing`` 换成只改启动方式的代理：forkserver 单线程、只预装载一次执行体模块，
每轮从它 fork——仍是独立进程、独立进程组、同样的 IPC 与强制回收路径。

冷启动契约（env / 出网守卫 / stdout guard / OTel 早于 SDK 装载）由
``tests/runtime/test_production_runtime.py`` 与 ``tests/test_layer_graph.py`` 用真实 spawn 覆盖。
"""

from __future__ import annotations

import multiprocessing
from multiprocessing import forkserver

from aidev_agent_cli.runtime import worker as worker_module

#: forkserver 预装载的模块：执行体（``SDK_ENTRY`` 的 module 部分），以及 ``openai.resources``——
#: 它由 ``OpenAI().chat`` 在构建 chat model 时惰性导入（含 admin/organization 等 pydantic 模型），
#: 每轮约 0.85s，是预装载后单轮构建的主要剩余开销。
PRELOAD = (worker_module.SDK_ENTRY.split(":", 1)[0], "openai.resources")


class ForkserverMultiprocessing:
    """``runtime.worker`` 只用 ``multiprocessing.get_context("spawn")``；其余属性原样转发。"""

    def __init__(self) -> None:
        self._context = multiprocessing.get_context("forkserver")
        self._context.set_forkserver_preload(list(PRELOAD))

    def get_context(self, method: str | None = None):
        assert method == "spawn", method
        return self._context

    def __getattr__(self, name: str):
        return getattr(multiprocessing, name)


def install() -> ForkserverMultiprocessing:
    """替换 worker 的启动方式，并立即拉起 forkserver（预装载与调用方后续启动并行）。"""
    shim = ForkserverMultiprocessing()
    worker_module.multiprocessing = shim
    forkserver.ensure_running()
    return shim