# -*- coding: utf-8 -*-
"""协议 adapter：把线上格式翻译成 ``infrastructure.ports.RuntimePort`` 调用，**不持第二份运行状态**。

- ``agui``：版本化帧的 12 方法路由、stdout guard、信号、OTel 与增量读循环。
- ``acp``：ACP 服务端 + AG-UI→ACP 解释器；``acp`` 与 ``acp-dsh``（供 dsh 等宿主驱动）以 ``AcpProfile`` 区分。
- ``streams``：与宿主进程之间的协议通道 ``ProtocolChannel``（由 ``__main__`` 构造后注入各 adapter）。

允许的依赖方向
--------------
- ``adapters`` → ``infrastructure``（运行时端口 ``ports``）+ ``hooks`` + ``utils``：允许
- ``adapters`` → ``runtime`` / ``sdk``：**禁止**（运行时实现由 ``__main__`` 组装后注入）
- ``adapters`` → ``resources``：**禁止**（那会让 adapter 拿到第二份资源/运行状态）
- 两条 adapter 互相 import：**禁止**

本 ``__init__`` 不 import 任何模块、不 re-export：各 adapter 顶部导入 SDK（``ag_ui`` / ``acp``）。
"""