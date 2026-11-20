# -*- coding: utf-8 -*-
"""协议 adapter：把线上格式翻译成 ``RuntimeService`` 调用，**不持第二份运行状态**。

- ``frames``：版本化帧构造器 + 串行化有界 ``FrameWriter``（AG-UI 帧信封）。
- ``acp_dsh``：ACP + ``_aidev/*`` 扩展（注入式后端，供 dsh 等宿主驱动；取代原 headless NDJSON）。
- ``agui``：版本化帧的 12 方法路由、stdout guard、信号、OTel 与增量读循环。
- ``acp``：ACP（Agent Client Protocol）服务端 + AG-UI→ACP 解释器。

允许的依赖方向
--------------
- ``adapters`` → ``runtime``（语义走 ``contracts`` / ``service``）+ ``infrastructure``：允许
- ``adapters`` → ``resources``：**禁止**（那会让 adapter 拿到第二份资源/运行状态）
- 两条 adapter 互相 import：**禁止**

本 ``__init__`` 不 import 任何模块、不 re-export：各 adapter 顶部导入 SDK（``ag_ui`` / ``acp``）。
"""
