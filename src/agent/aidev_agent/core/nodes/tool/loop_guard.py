# -*- coding: utf-8 -*-
"""同一 Trace 内「相同工具 + 相同参数」连续重复调用的检测、拒绝与终止。

判定键 = 工具名 + 参数指纹（规范化 JSON）。判定键在同一 run 的消息历史里连续出现
时累计次数：前 ``T`` 次放行真实执行，第 ``T+1`` 至 ``T+F`` 次拒绝并以失败反馈回给
模型，第 ``T+F+1`` 次直接终止本次 run。

拒绝不进入工具本体、不经过任何 wrapper（无副作用、不审批、不计时），但必须在
Trace 上留下一条以 ERROR 结束的 ``tool.execution`` span，让「循环拒绝」可按既有
``span_name='tool.execution'`` + ``error.type`` 口径统计。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from langchain_core.callbacks import AsyncCallbackManager, CallbackManager
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.prebuilt import ToolNode
from langgraph.prebuilt.tool_node import ToolCall, ToolRuntime

from aidev_agent.utils.tool_result import is_error_content

logger = logging.getLogger(__name__)

# 判定结论
ALLOW = "allow"
REJECT = "reject"
TERMINATE = "terminate"

# 终止原因文案：会话侧与调度层按该关键字归因「工具循环调用拒绝」
TERMINATION_MESSAGE = "任务因工具循环调用被中断"
TRUNCATED_SUFFIX = "……（内容过长，已截断）"


class ToolLoopRejection(RuntimeError):
    """被拒绝调用的错误载体，仅用于 span 的 ``error.type``（取值 ``ToolLoopRejection``）。"""


class ToolLoopTerminationError(RuntimeError):
    """连续重复达到 ``T+F+1`` 次时抛出，越过 ToolNode 兜底直接终止 run。"""


@dataclass(frozen=True)
class LoopGuardPolicy:
    """循环守卫策略。

    Attributes:
        repeat_limit: 允许真实执行的次数 ``T``，必须 >= 1。
        feedback_rounds: 拒绝后允许返回的失败反馈次数 ``F``，必须 >= 0。
        result_limit: 拒绝反馈中「最后一次真实返回内容」的摘要长度上限。
    """

    repeat_limit: int = 3
    feedback_rounds: int = 2
    result_limit: int = 10000


@dataclass(frozen=True)
class LoopDecision:
    """一次调用的判定结论。

    Attributes:
        action: ``allow`` / ``reject`` / ``terminate``。
        repeat_count: 含本次在内的连续调用次数。
        last_result: 该判定键最近一次真实执行返回的 ToolMessage，可能为 None。
    """

    action: str
    repeat_count: int = 0
    last_result: ToolMessage | None = None


def fingerprint(args: Any) -> str | None:
    """计算参数的规范化 JSON 指纹。

    对象键排序、紧凑分隔符、字符串原样；``null`` 与缺失字段的差异天然保留，
    ``1`` 与 ``"1"`` 因类型不同而不同。不可 JSON 化时返回 None，调用方按「不判定」处理。

    Args:
        args: 工具调用参数。

    Returns:
        规范化 JSON 字符串；无法序列化时为 None。
    """
    try:
        return json.dumps(args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        return None


def _call_key(call: ToolCall) -> tuple[str, str | None]:
    return str(call.get("name") or ""), fingerprint(call.get("args") or {})


def _extract_messages(state: Any, messages_key: str) -> list[AnyMessage]:
    """从工具节点拿到的状态里取出消息列表。"""
    if isinstance(state, dict):
        return list(state.get(messages_key) or [])
    if isinstance(state, (list, tuple)):
        return list(state)
    return list(getattr(state, messages_key, None) or [])


def _count_window(messages: list[AnyMessage]) -> list[AnyMessage]:
    """计数窗口：最后一条用户消息之后的消息；没有用户消息时退化为全量历史。"""
    for index in range(len(messages) - 1, -1, -1):
        if isinstance(messages[index], HumanMessage):
            return messages[index + 1 :]
    return messages


@dataclass
class _Slot:
    """计数序列中的一项：同一 AIMessage 内同判定键折叠为一项。"""

    key: tuple[str, str | None]
    tool_call_ids: set[str]


def _build_slots(window: list[AnyMessage]) -> list[_Slot]:
    slots: list[_Slot] = []
    for message in window:
        tool_calls = getattr(message, "tool_calls", None) if isinstance(message, AIMessage) else None
        if not tool_calls:
            continue
        batch: dict[tuple[str, str | None], _Slot] = {}
        for tool_call in tool_calls:
            key = _call_key(tool_call)
            slot = batch.get(key)
            if slot is None:
                slot = _Slot(key=key, tool_call_ids=set())
                batch[key] = slot
                slots.append(slot)
            if tool_call.get("id"):
                slot.tool_call_ids.add(tool_call["id"])
    return slots


def _nearest_result(window: list[AnyMessage], tool_call_ids: set[str]) -> ToolMessage | None:
    """在窗口内找出指定调用集合最近一条真实返回的 ToolMessage。"""
    if not tool_call_ids:
        return None
    for message in reversed(window):
        if isinstance(message, ToolMessage) and message.tool_call_id in tool_call_ids:
            return message
    return None


def count_contiguous(messages: list[AnyMessage], call: ToolCall, messages_key: str = "messages") -> int:
    """统计当前调用之前、同一判定键在本连续段内已出现的次数。

    规则：只统计最后一条用户消息之后的窗口；同一 AIMessage 内同一判定键折叠为一次；
    向前累计遇到不同工具名或不同参数指纹即归零（返回已累计次数）。当前调用按
    ``tool_call_id`` 在序列中定位，定位不到时按追加在末尾处理。

    Args:
        messages: 消息历史，接受原始列表或含 ``messages_key`` 的状态字典。
        call: 当前工具调用。
        messages_key: 状态字典中消息列表的键名。

    Returns:
        当前调用之前的连续出现次数 ``n``；当前调用参数不可指纹化时为 0（不判定）。
    """
    return _scan(messages, call, messages_key)[0]


def _scan(messages: Any, call: ToolCall, messages_key: str = "messages") -> tuple[int, ToolMessage | None]:
    window = _count_window(_extract_messages(messages, messages_key))
    current_key = _call_key(call)
    if current_key[1] is None:
        return 0, None

    slots = _build_slots(window)
    position = len(slots)
    for index, slot in enumerate(slots):
        if call.get("id") and call["id"] in slot.tool_call_ids:
            position = index
            break

    count = 0
    segment_ids: set[str] = set()
    index = position - 1
    while index >= 0 and slots[index].key == current_key:
        count += 1
        segment_ids |= slots[index].tool_call_ids
        index -= 1

    return count, _nearest_result(window, segment_ids)


def _truncate(content: Any, limit: int) -> str:
    text = content if isinstance(content, str) else str(content)
    if limit > 0 and len(text) > limit:
        return text[:limit] + TRUNCATED_SUFFIX
    return text


def _build_rejection_feedback(call: ToolCall, decision: LoopDecision, policy: LoopGuardPolicy) -> str:
    """构造拒绝反馈：工具名、已连续次数、拒绝结论、末次返回摘要、后续引导。"""
    tool_name = call.get("name") or ""
    repeat_count = decision.repeat_count
    last_result = decision.last_result
    last_summary = (
        _truncate(last_result.content, policy.result_limit) if last_result is not None else "（未能取到历史返回内容）"
    )
    return (
        f"工具 {tool_name} 以相同参数已连续调用 {repeat_count} 次且未产生新结果，"
        f"本次调用已被拒绝执行（未实际调用工具）。\n"
        f"工具名：{tool_name}\n"
        f"已连续调用次数：{repeat_count}\n"
        f"最后一次真实返回内容摘要：\n{last_summary}\n"
        f"请改用其他工具、调整调用参数，或直接结束任务并向用户说明当前进展；"
        f"继续以相同参数调用将被中断。"
    )


def _build_termination_message(call: ToolCall, decision: LoopDecision, policy: LoopGuardPolicy) -> str:
    tool_name = call.get("name") or ""
    return (
        f"{TERMINATION_MESSAGE}：工具 {tool_name} 以相同参数连续调用已达 {decision.repeat_count} 次"
        f"（真实执行 {policy.repeat_limit} 次，拒绝反馈 {policy.feedback_rounds} 次），"
        f"循环未收敛，本次任务终止。"
    )


def _rejection_payload(call: ToolCall, tool: Any) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """复刻 ``BaseTool.run`` 的回调载荷，保证 ``tool.name`` / ``tool.input`` 与真实调用同格式。"""
    serialized = {"name": call.get("name") or "", "description": getattr(tool, "description", "") or ""}
    inputs = call.get("args") or {}
    return serialized, str(inputs), inputs


def _emit_rejection_span_sync(config: dict | None, call: ToolCall, tool: Any, feedback: str) -> None:
    """同步路径：为被拒绝的调用派发一条以 ERROR 结束的工具 span。"""
    callbacks = (config or {}).get("callbacks")
    if not callbacks:
        return
    try:
        serialized, input_str, inputs = _rejection_payload(call, tool)
        manager = CallbackManager.configure(callbacks)
        run_manager = manager.on_tool_start(
            serialized,
            input_str,
            run_id=uuid4(),
            inputs=inputs,
            tool_call_id=call.get("id"),
            name=call.get("name"),
        )
        run_manager.on_tool_error(ToolLoopRejection(feedback))
    except Exception as error:  # noqa: BLE001 - span 派发失败不得影响拒绝结果
        logger.warning("[ToolLoopGuard] 拒绝调用的工具 span 派发失败: %s", error)


async def _emit_rejection_span_async(config: dict | None, call: ToolCall, tool: Any, feedback: str) -> None:
    """异步路径：语义与同步版本一致。"""
    callbacks = (config or {}).get("callbacks")
    if not callbacks:
        return
    try:
        serialized, input_str, inputs = _rejection_payload(call, tool)
        manager = AsyncCallbackManager.configure(callbacks)
        run_manager = await manager.on_tool_start(
            serialized,
            input_str,
            run_id=uuid4(),
            inputs=inputs,
            tool_call_id=call.get("id"),
            name=call.get("name"),
        )
        await run_manager.on_tool_error(ToolLoopRejection(feedback))
    except Exception as error:  # noqa: BLE001 - span 派发失败不得影响拒绝结果
        logger.warning("[ToolLoopGuard] 拒绝调用的工具 span 派发失败: %s", error)


def _mark_content_error(result: Any) -> Any:
    """内容路径失败标记：返回正文以 ``[ERROR]`` 开头时把 ToolMessage 置为失败。"""
    if isinstance(result, ToolMessage) and is_error_content(result.content):
        result.status = "error"
    return result


class LoopGuardToolNode(ToolNode):
    """在 ToolNode 执行前做连续重复判定的工具节点。

    放行时原样调用 ``super()._run_one`` / ``super()._arun_one``，工具身份、wrapper 链、
    ToolMessage 形态与 span 属性均与变更前一致；只有拒绝与终止走新增分支。
    """

    def __init__(self, *args: Any, loop_guard: LoopGuardPolicy | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.loop_guard = loop_guard

    def _decide(self, call: ToolCall, tool_runtime: ToolRuntime, policy: LoopGuardPolicy) -> LoopDecision:
        """判定当前调用；判定逻辑自身出错时一律按放行处理。"""
        try:
            messages = _extract_messages(tool_runtime.state, self._messages_key)
            count, last_result = _scan(messages, call, self._messages_key)
        except Exception as error:  # noqa: BLE001 - 判定失败不得阻断工具调用主链路
            logger.warning("[ToolLoopGuard] 重复调用判定失败，本次按放行处理: %s", error)
            return LoopDecision(action=ALLOW)

        repeat_count = count + 1
        if count < policy.repeat_limit:
            return LoopDecision(action=ALLOW, repeat_count=repeat_count)
        if count < policy.repeat_limit + policy.feedback_rounds:
            return LoopDecision(action=REJECT, repeat_count=repeat_count, last_result=last_result)
        return LoopDecision(action=TERMINATE, repeat_count=repeat_count, last_result=last_result)

    def _reject(self, call: ToolCall, feedback: str) -> ToolMessage:
        return ToolMessage(
            content=feedback,
            name=call.get("name"),
            tool_call_id=call.get("id"),
            status="error",
        )

    def _run_one(
        self,
        call: ToolCall,
        input_type: Any,
        tool_runtime: ToolRuntime,
    ) -> Any:
        tool = self.tools_by_name.get(call["name"])
        policy = self.loop_guard
        # 工具不存在时交回 ToolNode 走既有校验路径（无效工具名有独立错误消息）
        if policy is None or tool is None:
            return _mark_content_error(super()._run_one(call, input_type, tool_runtime))

        decision = self._decide(call, tool_runtime, policy)
        if decision.action == TERMINATE:
            raise ToolLoopTerminationError(_build_termination_message(call, decision, policy))
        if decision.action == REJECT:
            feedback = _build_rejection_feedback(call, decision, policy)
            _emit_rejection_span_sync(tool_runtime.config, call, tool, feedback)
            return self._reject(call, feedback)

        return _mark_content_error(super()._run_one(call, input_type, tool_runtime))

    async def _arun_one(
        self,
        call: ToolCall,
        input_type: Any,
        tool_runtime: ToolRuntime,
    ) -> Any:
        tool = self.tools_by_name.get(call["name"])
        policy = self.loop_guard
        if policy is None or tool is None:
            return _mark_content_error(await super()._arun_one(call, input_type, tool_runtime))

        decision = self._decide(call, tool_runtime, policy)
        if decision.action == TERMINATE:
            raise ToolLoopTerminationError(_build_termination_message(call, decision, policy))
        if decision.action == REJECT:
            feedback = _build_rejection_feedback(call, decision, policy)
            await _emit_rejection_span_async(tool_runtime.config, call, tool, feedback)
            return self._reject(call, feedback)

        return _mark_content_error(await super()._arun_one(call, input_type, tool_runtime))


__all__ = [
    "ALLOW",
    "REJECT",
    "TERMINATE",
    "LoopDecision",
    "LoopGuardPolicy",
    "LoopGuardToolNode",
    "ToolLoopRejection",
    "ToolLoopTerminationError",
    "count_contiguous",
    "fingerprint",
]
