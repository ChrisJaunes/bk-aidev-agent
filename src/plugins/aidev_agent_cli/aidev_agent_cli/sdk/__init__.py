# -*- coding: utf-8 -*-
"""aidev_agent SDK 适配层（worker 侧执行体）的**空包根**。

这里**没有任何** re-export：``sdk`` 是本包唯一「装载 SDK 执行体」的命名空间，
父进程侧（``runtime`` / ``adapters`` / ``hooks`` / ``resources`` / ``infrastructure`` / ``utils``）
一律不得引用它的任何子模块。worker 的执行体通过 import target 直接装载：

    aidev_agent_cli.sdk.executor:build

跨 spawn 边界传递的只有这个 ``module:name`` 串（``runtime.worker.SDK_ENTRY``）与 JSON 快照（见
:mod:`aidev_agent_cli.runtime.worker`）。执行体 ``executor`` 顶部的 SDK 依赖因此只在
``_worker_entry`` 完成 ``install_worker_startup``（环境发布 → stdout guard → 出网限制 →
``sys.path`` 加固 → OTel）之后才被导入——那是 ``ag_ui`` 导入期 print 不污染协议 stdout、
OTel monkey-patch 生效的前提。

父进程要的值对象在 :mod:`aidev_agent_cli.runtime.models`，终止事件形状在
:mod:`aidev_agent_cli.utils.terminal`（纯 stdlib）。从本包根取任何名字都会拉起 SDK，
这是刻意的物理边界，不是疏忽。
"""

from __future__ import annotations

__all__: list[str] = []