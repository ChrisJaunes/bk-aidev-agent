# -*- coding: utf-8 -*-
"""Hook 往返的值对象：worker 发 ``HookRequest``，父进程 handler 回 ``HookReply``。

只依赖标准库，父进程与 worker 两侧都可安全导入；取消信号以 ``CancelSignal`` 协议表达，
``runtime.handles.CancelToken`` 天然满足它，hooks 因此不反向依赖 runtime。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal, Protocol

if TYPE_CHECKING:
    from .contract import HookConfig


class CancelSignal(Protocol):
    """handler 等待裁决期间巡检的取消信号。"""

    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True)
class HookRequest:
    line_id: str
    params: dict[str, Any]
    timeout_ms: int


@dataclass(frozen=True)
class HookReply:
    status: Literal["reply", "timeout", "cancelled"]
    payload: dict[str, Any] | None = None


HookHandler = Callable[[HookRequest, CancelSignal], Awaitable[HookReply]]




class ProcessHooks(Protocol):
    """进程级 hook：``RuntimeService`` 持有一份，``config`` 写进每轮快照，调用本身应答 worker 的 hook 请求。"""

    config: "HookConfig"

    async def __call__(self, request: HookRequest, token: CancelSignal) -> HookReply: ...


__all__ = ["CancelSignal", "HookHandler", "ProcessHooks", "HookReply", "HookRequest"]
