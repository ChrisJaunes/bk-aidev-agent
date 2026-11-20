# -*- coding: utf-8 -*-
"""Hook 子系统：hook 配置解析与父进程侧处理。

* ``contract`` —— 纯值契约（点位名、降级原因、``HookConfig``），标准库实现；
* ``models`` —— 跨进程往返的值对象（``HookRequest`` / ``HookReply`` / ``HookHandler``）；
* ``handler`` —— 进程级 handler，``__main__`` 解析 ``--hooks`` 构造后交给 adapter；

worker 侧的 SDK 接线（``HeadlessHookBus`` / ``build_hook_set``）在 :mod:`aidev_agent_cli.sdk.hooks`。

包根刻意不 re-export。
"""

from __future__ import annotations

__all__: list[str] = []