# -*- coding: utf-8 -*-
"""adapter 所见的运行时端口。

- ``ports``：``RuntimePort``（实现是 ``runtime.service.RuntimeService``，由 ``__main__`` 注入 adapter）。

其余原先住在这里的设施已按职责归位：通用工具与跨层值契约在 ``utils``，协议通道在
``adapters.streams``，进程级配置在 ``runtime.settings``，启动纪律在 ``bootstrap``。

允许的依赖方向：``infrastructure`` → ``utils``；→ ``runtime`` / ``adapters`` / ``resources`` / ``sdk`` 禁止。
"""
