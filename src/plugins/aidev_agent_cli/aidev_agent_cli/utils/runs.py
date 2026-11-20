# -*- coding: utf-8 -*-
"""Run requests, session options, immutable outcomes and session information."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable
from uuid import uuid4

from .inputs import ChatInput, ContextInput, ResumeInterrupt

#: 终态取值。``rejected`` 不是「跑完之后的状态」，而是「这次 start 被拒绝」——
#: 它同样只落定一次，因此仍属终态的一种。
RunStatus = Literal["finished", "cancelled", "interrupted", "failed", "rejected"]


@dataclass(frozen=True)
class RunOutcome:
    """The single terminal outcome of a run; resource queries optionally carry data."""

    status: RunStatus
    run_id: str = ""
    session_code: str = ""
    error_code: str = ""
    error_message: str = ""
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    turn_id: str = ""
    data: Any = None

    @property
    def ok(self) -> bool:
        """这一轮是否「正常结束」（含中断轮——中断是结果，不是失败）。"""
        return self.status in ("finished", "interrupted")


@runtime_checkable
class EventSink(Protocol):
    """Per-run asynchronous typed event destination."""

    async def send(self, event: Any) -> None: ...


@dataclass(frozen=True)
class SessionState:
    """跨轮会话状态：runtime 持有与持久化，每轮随快照交给 worker，worker 合并本轮产物后传回。

    sdk 只做"本轮 + 上一轮状态 → 合并后的状态"；是否提交（失败 / 取消整体回滚）由 runtime 决定。
    """

    records: list[dict[str, Any]] = field(default_factory=list)
    interrupts: list[dict[str, Any]] = field(default_factory=list)
    turn_count: int = 0
    hook_memo: dict[str, str] = field(default_factory=dict)
    hook_sequence: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "SessionState":
        data = data or {}
        return cls(
            records=list(data.get("records", [])),
            interrupts=list(data.get("interrupts", [])),
            turn_count=int(data.get("turn_count", 0)),
            hook_memo=dict(data.get("hook_memo", {})),
            hook_sequence=int(data.get("hook_sequence", 0)),
        )


@dataclass(frozen=True)
class IdentitySnapshot:
    """一次 admission 捕获的用户身份；access_token 进快照但不进 repr。"""

    username: str
    access_token: str = field(default="", repr=False)
    revision: int = 0


@dataclass(frozen=True)
class LaunchOptions:
    """本次启动的配置（__main__ 解析，RuntimeService 消费）。

    model / session / resources 是**默认值**：会话请求里显式给出的值优先。
    """

    #: 默认模型（-m / --model）；空 = 由资源来源决定。
    model: str = ""
    #: 默认会话 code（-s / --session）；请求不带会话时使用。
    session: str = ""
    #: 叠加的资源来源（--resources，已展开）；空 = 协议默认值。
    resources: tuple[str, ...] = ()
    #: 工作区根：本次启动时的 cwd。
    workspace_root: str = field(default_factory=os.getcwd)


@dataclass(frozen=True)
class SessionOptions:
    """Cross-turn configuration, without transport objects."""

    session_code: str
    agent_code: str = ""
    model: str = ""
    username: str = ""
    base_url: str = ""
    auth_headers: dict[str, str] | None = field(default=None, repr=False)
    temperature: float | None = None
    max_tokens: int | None = None
    resources: tuple[str, ...] = ()
    tool_factories: tuple[str, ...] = ()


@dataclass(frozen=True)
class SessionInfo:
    session_code: str
    created_at: str
    closed: bool
    revision: int

    def record(self) -> dict[str, str]:
        return {"sessionCode": self.session_code, "createdAt": self.created_at}


@dataclass(frozen=True)
class RunRequest:
    session_code: str
    input: ChatInput | ContextInput = field(default_factory=lambda: ChatInput(""))
    execute_options: dict[str, Any] = field(default_factory=dict)
    correlation_id: str = ""


@dataclass(frozen=True)
class ResumeRequest:
    session_code: str
    input: ResumeInterrupt
    correlation_id: str = ""


def new_session_code() -> str:
    """Create a new session identity."""
    return f"thread-{uuid4().hex[:12]}"