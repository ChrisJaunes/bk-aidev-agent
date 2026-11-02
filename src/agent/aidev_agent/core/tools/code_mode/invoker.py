# -*- coding: utf-8 -*-
"""内层工具执行器：把代码里的函数调用还原成既有工具执行链上的一次调用。

每次内层调用复用装配期构造的「内层 ToolNode」——脱敏、不可信净化、结果截断、
去重、计时与审批包装全部走既有实现（REQ-4），不另起旁路。内层调用不写图状态、
不产生 ``on_tool_*`` 回调（子 config 清空 callbacks）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Callable, Iterable, Sequence

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

#: Pregel 运行时在 config 里的私有键（与 langgraph 的 ``CONFIG_KEY_RUNTIME`` 取值一致）。
_RUNTIME_CONFIG_KEY = "__pregel_runtime"

#: 内层调用事件发射器：(tool_name, args, call_id, config) -> None
EventEmitter = Callable[..., None]


def _resolve_runtime(config: RunnableConfig | None) -> Runtime:
    """取图运行时；脱离图调用（单测）时构造一个无 store 的空运行时。"""

    runtime = ((config or {}).get("configurable") or {}).get(_RUNTIME_CONFIG_KEY)
    if runtime is not None:
        return runtime
    return Runtime(store=None, context=None)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


def _child_config(config: RunnableConfig | None) -> RunnableConfig:
    """复制当前 config 并清空 callbacks，避免内层工具事件被 AG-UI 适配器重复解释。"""

    child: dict = dict(config or {})
    child.pop("callbacks", None)
    child.pop("run_name", None)
    return child  # type: ignore[return-value]


def _extract_tool_message(result: Any, call_id: str) -> ToolMessage | None:
    messages: Sequence[Any] = ()
    if isinstance(result, dict):
        messages = result.get("messages") or ()
    elif isinstance(result, list):
        messages = result
    for message in reversed(list(messages)):
        if isinstance(message, ToolMessage) and getattr(message, "tool_call_id", None) == call_id:
            return message
    for message in reversed(list(messages)):
        if isinstance(message, ToolMessage):
            return message
    return None


class CodeModeInvoker:
    """按工具名把一次内层调用路由到内层 ToolNode。"""

    def __init__(
        self,
        *,
        tools: Iterable[BaseTool],
        tool_node: ToolNode | None,
        event_emitter: EventEmitter | None = None,
    ) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._tool_node = tool_node
        self._event_emitter = event_emitter

    @property
    def tool_names(self) -> set[str]:
        return set(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    async def ainvoke(
        self,
        name: str,
        args: Any,
        *,
        call_id: str,
        config: RunnableConfig | None = None,
        state: dict | None = None,
    ) -> tuple[str | None, str | None]:
        """执行一次内层调用，返回 ``(content, error)``；未装配的工具不执行本体。"""

        if name not in self._tools:
            return None, f"unknown tool: {name!r}（不在本会话的可调用集合内）"
        if self._tool_node is None:
            return None, f"tool {name!r} 无法执行：内层工具节点未装配"
        if not isinstance(args, dict):
            return None, f"invalid arguments for tool {name!r}: 期望对象"

        if self._event_emitter is not None:
            try:
                self._event_emitter(tool_name=name, args=args, call_id=call_id, config=config)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[code-mode] 内层调用事件发射失败: %s", exc)

        tool_call = {"name": name, "args": args, "id": call_id, "type": "tool_call"}
        message = AIMessage(content="", tool_calls=[tool_call])
        graph_state = dict(state or {})
        graph_state["messages"] = [message]
        try:
            result = await self._tool_node.ainvoke(
                graph_state, config=_child_config(config), runtime=_resolve_runtime(config)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[code-mode] 内层工具 %s 执行异常: %s", name, exc)
            return None, f"{type(exc).__name__}: {exc}"

        tool_message = _extract_tool_message(result, call_id)
        if tool_message is None:
            return None, f"tool {name!r} did not return a result"
        content = _content_to_text(tool_message.content)
        if getattr(tool_message, "status", None) == "error":
            return None, content
        return content, None


__all__ = ["CodeModeInvoker"]
