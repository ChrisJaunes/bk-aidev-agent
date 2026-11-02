# -*- coding: utf-8 -*-
"""code mode 的装配期入口：工具面收窄、精简清单、提示注入与内层调用事件。

- :func:`resolve_tool_use_mode`：开关取值的防御性校验（``model_construct`` 绕过
  pydantic 时也不会带着未知值装配）。
- :func:`prepare_inner_tools`：内层可调用集合 = 加载模式过滤（强制
  ``dontInterrupt``）× ``tool_deny`` × ``tool_allow``。
- :class:`CodeModePromptMiddleware`：把精简清单拼到 system prompt 尾部。
- :func:`emit_tool_call`：内层调用的 AG-UI 三元组事件（不新增事件类型）。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, List

from langchain_core.callbacks import dispatch_custom_event
from langchain_core.tools import BaseTool

from aidev_agent.core.ag_ui.types import CustomEventNames
from aidev_agent.core.nodes.model.pydantic_models import NextFunction, ProcessorContext
from aidev_agent.core.tools.code_mode.manifest import (
    ToolManifestEntry,
    build_manifest,
    render_manifest_prompt,
)

from .tool_loading_mode import MODE_DONT_INTERRUPT, filter_tools_by_loading_mode

logger = logging.getLogger(__name__)

TOOL_USE_MODE_FUNCTION_CALL = "function_call"
TOOL_USE_MODE_CODE_MODE = "code_mode"
TOOL_USE_MODES = frozenset({TOOL_USE_MODE_FUNCTION_CALL, TOOL_USE_MODE_CODE_MODE})


def resolve_tool_use_mode(execute_kwargs: Any) -> str:
    """读取并校验 ``ExecuteKwargs.tool_use_mode``；未知取值直接拒绝装配。"""

    value = getattr(execute_kwargs, "tool_use_mode", TOOL_USE_MODE_FUNCTION_CALL)
    if value not in TOOL_USE_MODES:
        raise ValueError(f"tool_use_mode 取值非法: {value!r}，合法取值: {sorted(TOOL_USE_MODES)}")
    return value


def apply_tool_permissions(
    tools: Iterable[BaseTool], *, tool_allow: Iterable[str] | None = None, tool_deny: Iterable[str] | None = None
) -> List[BaseTool]:
    """按 ``tool_deny`` → ``tool_allow`` 裁剪（与 ``ToolFilterMiddleware`` 同口径）。"""

    kept = list(tools)
    deny = set(tool_deny or ())
    allow = set(tool_allow or ())
    if deny:
        kept = [tool for tool in kept if tool.name not in deny]
    if allow:
        kept = [tool for tool in kept if tool.name in allow]
    return kept


def prepare_inner_tools(
    tools: Iterable[BaseTool],
    *,
    mode: str,
    tool_allow: Iterable[str] | None = None,
    tool_deny: Iterable[str] | None = None,
) -> List[BaseTool]:
    """code 环境内的可调用集合（REQ-4 / REQ-5）。

    无论调用方 ``mode`` 取什么值，ask 类工具与带生效审批配置的工具都不进代码环境：
    这里强制再按 ``dontInterrupt`` 过滤一次（幂等），再叠加 allow/deny。
    """

    no_interrupt = filter_tools_by_loading_mode(list(tools), MODE_DONT_INTERRUPT)
    return apply_tool_permissions(no_interrupt, tool_allow=tool_allow, tool_deny=tool_deny)


def build_code_mode_manifest(inner_tools: Iterable[BaseTool]) -> List[ToolManifestEntry]:
    return build_manifest(inner_tools)


def render_prompt(
    manifest: List[ToolManifestEntry],
    *,
    runtime_description: str = "",
    max_tool_calls: int,
    execute_timeout: int,
    max_retries: int,
) -> str:
    return render_manifest_prompt(
        manifest,
        runtime_description=runtime_description,
        max_tool_calls=max_tool_calls,
        execute_timeout=execute_timeout,
        max_retries=max_retries,
    )


def emit_tool_call(*, tool_name: str, args: dict, call_id: str, config: Any) -> None:
    """为一次内层工具调用发出 ``manually_emit_tool_call`` 事件。

    适配器把它转成一对 ``TOOL_CALL_START/ARGS/END``；事件只用于展示与审计，
    不回填模型上下文（REQ-6 / REQ-7）。
    """

    dispatch_custom_event(
        CustomEventNames.ManuallyEmitToolCall.value,
        data={"id": call_id, "name": tool_name, "args": json.dumps(args, ensure_ascii=False, default=str)},
        config=config,
    )


@dataclass
class CodeModePromptMiddleware:
    """把 code mode 说明与精简工具清单拼到 system prompt 尾部。"""

    prompt: str

    def __call__(self, ctx: ProcessorContext, next: NextFunction) -> None:
        ctx.prompt_slots.system = (ctx.prompt_slots.system or "") + self.prompt
        next()


__all__ = [
    "TOOL_USE_MODE_CODE_MODE",
    "TOOL_USE_MODE_FUNCTION_CALL",
    "TOOL_USE_MODES",
    "CodeModePromptMiddleware",
    "apply_tool_permissions",
    "build_code_mode_manifest",
    "emit_tool_call",
    "prepare_inner_tools",
    "render_prompt",
    "resolve_tool_use_mode",
]
