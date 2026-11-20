# -*- coding: utf-8 -*-
"""与业务无关的通用工具与跨层值契约：只依赖标准库与本包自身，不依赖任何其他层。

- ``logging`` / ``process`` / ``stdio``：日志配置、信号与守护线程执行器、stdout guard。
- ``errors`` / ``inputs`` / ``runs``：错误码与结构化异常、typed 输入、请求 / 会话选项 / 终态值对象。
- ``terminal``：AG-UI 终止事件字典（父进程与 worker 同形）。
- ``history``：本地 SQLite 运行事实（连接由调用方持有）。

本 ``__init__`` 不 import、不 re-export 任何模块。
"""
