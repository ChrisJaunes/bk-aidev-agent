# -*- coding: utf-8 -*-
"""单轮运行的输入：组装 agent 所需的全部配置（``AgentSpec``）与运行期回调端口（``TurnPorts``）。

两者都由上层（worker 侧的 ``executor``，其数据来自 runtime 快照）给定；sdk 不读进程环境、不认识本机目录约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, TypeVar

T = TypeVar("T")


@dataclass
class AgentSpec:
    session_code: str = ""
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
    #: 已解析的 ``HookConfig``；``None`` 表示未启用 hook。
    hooks: Any = None
    tools: list | None = None
    #: SDK checkpoint 文件；由 runtime 决定位置。
    checkpoint_path: str = ""
    #: LLM 限流重试次数与等待预算（秒）；0 表示不重试。由 runtime 从进程配置解析。
    rate_limit_retries: int = 0
    rate_limit_wait_budget: float = 0.0


class TurnPorts(Protocol):
    """运行期与宿主的交互面：取消、hook 往返、构建阶段上报。"""

    def cancelled(self) -> bool: ...

    def hook(self, **payload: Any) -> tuple[str, dict[str, Any] | None]: ...

    def building(self, build: Callable[[], T]) -> T: ...


class LocalPorts:
    """无宿主时的端口：从不取消、不支持 hook、构建不上报阶段。"""

    def cancelled(self) -> bool:
        return False

    def hook(self, **payload: Any) -> tuple[str, dict[str, Any] | None]:
        raise ValueError("enabled hooks require a worker request channel")

    def building(self, build: Callable[[], T]) -> T:
        return build()


__all__ = ["AgentSpec", "LocalPorts", "TurnPorts"]
