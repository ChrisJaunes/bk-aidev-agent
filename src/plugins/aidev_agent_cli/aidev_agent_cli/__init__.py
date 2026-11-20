# -*- coding: utf-8 -*-
"""Lightweight CLI entry; SDK execution lives in spawned aidev_agent_cli.sdk workers.

本模块的导入开销必须是**零副作用**且**不加载 SDK**：

* 不发生日志文件 / 目录创建（``utils.logging`` 不在导入期配置日志）；
* 不加载 ``ag_ui`` / ``aidev_agent`` / ``pydantic`` / 协议 SDK ``acp``；
* 不加载任何 ``runtime`` / ``adapters`` / ``resources`` 子模块。

因此 ``main`` 是**延迟代理**：真正的入口逻辑（含 adapter、runtime 组装与启动钩子）在
:mod:`aidev_agent_cli.__main__`，只在该函数第一次被调用时导入——``python -m
aidev_agent_cli`` 走的是同一入口，控制台脚本指向 ``aidev_agent_cli:main``。
"""

from __future__ import annotations

import importlib
from typing import Any

__version__ = "0.1.0"
__author__ = "tencentblueking"

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """进程入口：把调用转给 :func:`aidev_agent_cli.__main__.main`。

    延迟到调用点的理由不是循环：``__main__`` 会导入 adapter 与 runtime（其中 ``acp`` 是
    可选 extra、runtime 组装会读配置），而"import aidev_agent_cli"本身必须保持轻量。
    """
    entry: Any = _entry_module().main

    return entry(argv)


def _entry_module():
    """显式装载入口模块 ``aidev_agent_cli.__main__``（唯一的动态装载点）。"""
    return importlib.import_module("aidev_agent_cli.__main__")