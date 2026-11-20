# -*- coding: utf-8 -*-
"""Real SDK factory access, confined to the SDK integration package."""

from __future__ import annotations

from logging import getLogger
from typing import Any

from aidev_agent.config import settings
from aidev_agent.enums import AgentBuildType, AgentType
from aidev_agent.services.agent.factory import agent_factory as sdk_agent_factory
from aidev_agent.services.event_handlers import agui_writer
from aidev_agent.services.messages_handler.factory import message_handler_factory
from aidev_agent.services.messages_handler.in_memory import InMemoryQueueMessageHandler

logger = getLogger(__name__)


def build_agent(
    rm: Any,
    session_code: str,
    agent_code: str,
    username: str | None = None,
    *,
    checkpointer: Any | None = None,
    event_handler: Any | None = None,
    resume: Any = None,
    build_type: AgentBuildType = AgentBuildType.SESSION,
    **params: Any,
) -> Any:
    """Create or resume the real SDK agent using accepted worker-owned resources and writer."""
    user = username if username is not None else getattr(rm, "username", "")

    # 会话回写处理器不是可选项。`ChatCompletionAgent`
    # `_prepare_pre_run_history` 只有在 `event_handler` 是 `BaseSessionWriter`
    # 时才会继续，否则提前返回，而这个提前返回恰好决定了本轮的用户输入能否进入
    # 消息列表——没有它，这一轮会以
    # `ValueError: The messages list cannot be empty.` 失败。
    #
    # 事件处理器：调用方给的优先（同步执行路径的 SessionEventWriter）；否则用平台回写
    # writer——``_prepare_pre_run_history`` 只在 ``BaseSessionWriter`` 下继续，缺了它本轮
    # 以 ``ValueError: The messages list cannot be empty.`` 失败。
    writer = event_handler
    if writer is None:
        writer = agui_writer.AGUISessionWriter(
            session_code=session_code,
            client=rm.get_client(),
            username=user,
        )

    # 唯一构建入口：SDK 的 agent_factory 注入点（create / resume 同一组参数）。
    # 用 SESSION 而非 DIRECT（DIRECT 只给显式 session_context_data 的无状态轮）：
    # SESSION 会经 ``resource_manager.get_chat_session_contents(session_code)`` 加载历史，
    # 中断恢复因此能在 chat_history 里找到 role=INTERRUPT 记录。
    # None 值不下发：保留 SDK 自己的默认值（如 max_tokens 的 settings.MAX_TOKENS）。
    build = {
        "agent_code": agent_code,
        "agent_type": AgentType.CHAT,
        "build_type": build_type,
        "session_code": session_code,
        "resource_manager": rm,
        "username": user,
        "checkpointer": checkpointer,
        "event_handler": writer,
        **{key: value for key, value in params.items() if value is not None},
    }
    factory = sdk_agent_factory()

    if resume is not None:
        build.pop("session_code")
        return factory.resume(session_code=session_code, resume=resume, **build)

    return factory.create(**build)


def ensure_session(
    rm: Any,
    session_code: str,
    *,
    session_name: str = "agent-tui",
) -> None:
    """幂等地让平台知晓 ``session_code``。

    worker 以前只在本地生成 ``thread-<uuid>`` 且不告知任何人。平台随后就没有该会话
    的任何记录——这就是列出会话总是返回空、以及针对一个全新 code 的首轮会以
    ``.../chat/session/<code>/`` 返回 404 失败的原因。
    """
    rm.get_or_create_session(
        session_code=session_code,
        session_name=session_name,
        protocol_version="",
    )


def describe_agent(agent: Any) -> str:
    """供日志面板使用的单行、不含密钥的摘要。"""
    tools = getattr(agent, "tools", None) or []
    names = [getattr(t, "name", str(t)) for t in tools]

    return (
        f"agent={type(agent).__name__} "
        f"agent_code={getattr(settings, 'APP_CODE', '?')} "
        f"model={getattr(agent, 'model_name', '?')} "
        f"tools={names}"
    )


def validate_message_handler() -> None:
    """取消依赖内存消息处理器；非内存实现时显式失败退出。"""
    handler = message_handler_factory()
    if not isinstance(handler, InMemoryQueueMessageHandler):
        raise SystemExit(
            f"aidev_agent_cli 需要内存消息处理器（InMemoryQueueMessageHandler），"
            f"当前为 {type(handler).__name__}；请设置 MESSAGE_HANDLER_TYPE=inmemory"
        )
