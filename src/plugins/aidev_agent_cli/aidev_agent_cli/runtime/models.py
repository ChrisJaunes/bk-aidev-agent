# -*- coding: utf-8 -*-
"""快照与 hook 的**纯值对象**：parent 与 worker 两侧共用的唯一数据形状。

为什么单独一个模块
------------------
这些 dataclass 会 ``asdict()`` 后写进 worker 快照（JSON IPC）——它们是**协议数据**，
不是 SDK 执行体的实现细节。父进程要造快照、要判 hook 回复，却**不需要** SDK：把值对象
留在 ``runtime.sdk`` 里会迫使每一个想拿 dataclass 的模块延迟 import 整个 SDK 执行体
（那就是 ``auth → sdk → auth`` 潜在环与遍地函数内 import 的根源）。因此值对象住在这里，
``runtime.sdk.executor`` 从这边取，方向永远是 ``sdk → models``，不会被反转。

字段契约：字段名 / 顺序 / 默认值 / ``repr=False`` 都是线上形状的一部分（``asdict()``
直接进 IPC 快照），改动即协议变更。``access_token`` / ``env`` / ``auth_headers`` 永远
``repr=False``——它们进快照，但不进日志、不进 repr。

依赖边界：本模块只允许标准库 + ``.contracts`` + ``.inputs``。**不得** import ``.worker`` /
``.sdk`` / ``..infrastructure`` / ``..resources``——本模块是「无 SDK、无副作用」叶子，
``tests/test_layer_graph.py`` 用它做无 SDK 断言。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Awaitable, Callable, Literal

from .contracts import CancelToken
from .inputs import ChatInput, ContextInput, ResumeInterrupt


def event_payload(event: Any) -> Any:
    """AG-UI 事件 → 线上 JSON 载荷（与 SSE 编码同形）；已是 dict 的原样返回。"""
    if hasattr(event, "model_dump"):
        return event.model_dump(mode="json", by_alias=True, exclude_none=True)
    return event


@dataclass(frozen=True)
class IdentitySnapshot:
    username: str
    access_token: str = field(default="", repr=False)
    revision: int = 0


@dataclass(frozen=True)
class SettingsSnapshot:
    cwd: str
    env: dict[str, str] = field(default_factory=dict, repr=False)
    virtual_mode: bool = True
    revision: int = 0


@dataclass(frozen=True)
class SessionSnapshot:
    session_code: str
    agent_code: str
    resources: tuple[str, ...]
    identity: IdentitySnapshot
    settings: SettingsSnapshot
    model: str = ""
    base_url: str = ""
    auth_headers: dict[str, str] | None = field(default=None, repr=False)
    temperature: float | None = None
    max_tokens: int | None = None
    tool_factories: tuple[str, ...] = ()


@dataclass(frozen=True)
class TurnSnapshot:
    session: SessionSnapshot
    kind: str
    correlation_id: str
    run_id: str
    input: ChatInput | ContextInput | ResumeInterrupt
    execute_options: dict[str, Any] = field(default_factory=dict)
    hooks: dict[str, Any] | None = None

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class HookRequest:
    line_id: str
    params: dict[str, Any]
    timeout_ms: int


@dataclass(frozen=True)
class HookReply:
    status: Literal["reply", "timeout", "cancelled"]
    payload: dict[str, Any] | None = None


HookHandler = Callable[[HookRequest, CancelToken], Awaitable[HookReply]]


__all__ = [
    "HookHandler",
    "HookReply",
    "HookRequest",
    "IdentitySnapshot",
    "SessionSnapshot",
    "SettingsSnapshot",
    "TurnSnapshot",
    "event_payload",
]
