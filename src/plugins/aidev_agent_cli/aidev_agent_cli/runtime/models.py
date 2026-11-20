# -*- coding: utf-8 -*-
"""快照与 hook 的**纯值对象**：parent 与 worker 两侧共用的唯一数据形状。

为什么单独一个模块
------------------
这些 dataclass 会 ``asdict()`` 后写进 worker 快照（JSON IPC）——它们是**协议数据**，
不是 SDK 执行体的实现细节。父进程要造快照、要判 hook 回复，却**不需要** SDK：把值对象
留在 ``sdk`` 里会迫使每一个想拿 dataclass 的模块延迟 import 整个 SDK 执行体
（那就是 ``auth → sdk → auth`` 潜在环与遍地函数内 import 的根源）。因此值对象住在这里，
``sdk.executor`` 从这边取，方向永远是 ``sdk → models``，不会被反转。

字段契约：字段名 / 顺序 / 默认值 / ``repr=False`` 都是线上形状的一部分（``asdict()``
直接进 IPC 快照），改动即协议变更。``access_token`` / ``env`` / ``auth_headers`` 永远
``repr=False``——它们进快照，但不进日志、不进 repr。

依赖边界：本模块只允许标准库 + ``utils.inputs`` / ``utils.runs``。**不得** import ``.worker`` /
``.sdk`` / ``..resources`` 等 utils 以外的模块——本模块是「无 SDK、无副作用」叶子，
``tests/test_layer_graph.py`` 用它做无 SDK 断言。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from ..utils.inputs import ChatInput, ContextInput, ResumeInterrupt
from ..utils.runs import SessionState


@dataclass(frozen=True)
class ProcessSnapshot:
    """worker 进程的启动环境：``runtime.worker._worker_entry`` 在装载 SDK 之前发布（不属于 agent 组装）。"""

    cwd: str
    env: dict[str, str] = field(default_factory=dict, repr=False)
    revision: int = 0


@dataclass(frozen=True)
class AgentSnapshot:
    """单轮组装 agent 的配置，字段与 ``sdk.spec.AgentSpec`` 一一对应。

    只有两处是"可跨进程"的形态：``tool_factories``（``module:name`` 导入目标，worker 侧实例化成 ``tools``）
    与 ``hooks``（``HookConfig.snapshot()`` 字典，worker 侧解析）。
    """

    session_code: str
    agent_code: str = ""
    resources: tuple[str, ...] = ()
    model: str = ""
    username: str = ""
    access_token: str = field(default="", repr=False)
    credentials: dict[str, str] = field(default_factory=dict, repr=False)
    base_url: str = ""
    auth_headers: dict[str, str] | None = field(default=None, repr=False)
    temperature: float | None = None
    max_tokens: int | None = None
    hooks: dict[str, Any] | None = None
    tool_factories: tuple[str, ...] = ()
    #: SDK checkpoint 文件；runtime 解析（来自 ``ProcessConfig.checkpoint_path``）。
    checkpoint_path: str = ""
    rate_limit_retries: int = 0
    rate_limit_wait_budget: float = 0.0


@dataclass(frozen=True)
class TurnSnapshot:
    agent: AgentSnapshot
    process: ProcessSnapshot
    kind: str
    correlation_id: str
    run_id: str
    input: ChatInput | ContextInput | ResumeInterrupt
    execute_options: dict[str, Any] = field(default_factory=dict)
    #: 上一轮提交后的会话状态；worker 合并本轮产物后传回，runtime 决定是否提交。
    state: SessionState = field(default_factory=SessionState, repr=False)
    #: resume 时 runtime 已在 pending 中命中的中断（平台来源且无 pending 时为 None）。
    resume_target: dict[str, Any] | None = None
    #: admission 已判定失败的轮次（``{"code", "message"}``）：不起 worker，直接以 RUN_ERROR 落定。
    rejection: dict[str, str] | None = None
    #: 本轮采用的身份版本（父进程记账，worker 不读）。
    identity_revision: int = 0

    def payload(self) -> dict[str, Any]:
        return asdict(self)


__all__ = [
    "AgentSnapshot",
    "ProcessSnapshot",
    "TurnSnapshot",
]