# -*- coding: utf-8 -*-
"""与业务无关的进程设施：配置、日志、SQLite saver 与 egress 限制。

- ``config``：``~/.bkai/bkai_tui.json``、环境引导、凭据与 ACP token 解析。
- ``logging``：BKAI 状态路径与文件/stderr 日志配置（原 ``utils``，逐字改名）。
- ``checkpointer``：跨 SDK 事件循环的持久 SQLite checkpoint（逐字保留）。
- ``network_guard``：可选的评测出网限制；默认关闭（逐字保留）。
- ``open_telemetry``：agent 追踪埋点（OTel），由入口启动钩子为所有协议安装。

为什么单独成层：这些设施（日志命名空间、本机配置目录、saver、egress）是「换掉 UI 也不该
换掉」的东西，放在这里能说明它们不是某个 adapter 的业务。

允许的依赖方向
--------------
- ``infrastructure`` 内部：``config`` → ``logging``（唯一一条）；``checkpointer`` /
  ``network_guard`` / ``logging`` 不互相 import
- ``infrastructure`` → ``runtime`` / ``adapters`` / ``resources``：**禁止**（``logging`` 是叶子）

本 ``__init__`` 不 import 任何模块、不 re-export：``logging`` 在导入期就执行
``configure_logging()``（安装 handler、建日志目录），re-export 会把这些副作用带进
``import aidev_agent_cli``。
"""
