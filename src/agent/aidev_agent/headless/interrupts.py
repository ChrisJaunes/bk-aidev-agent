# -*- coding: utf-8 -*-
"""headless 中断编排：裁决权威是宿主负载，不触达平台（REQ-17 / ACC-22）。

两处去平台化：

- ``dispatch_interrupts``（流结束侧）：只把 ``state.tasks`` 的 pending 中断归一为
  ``InterruptOutcome(status="built")``，**不 prepare、不建单、不 enrich**；
- ``get_resume_input``（续流侧）：只读 ``tasks``，以 reason 白名单
  （``aidev:hook_ask`` / ``aidev:user_question``）判定，构造 ``Command(resume=...)``，
  **不查 DB、不调平台 API**。

可恢复范围（本期明确边界）：hook ``ask`` 与工具向用户提问可恢复；ITSM 权限审批
（``aidev:tool_approval``）不接线，遇显式失败 ``resume_interrupt_unsupported`` ——
用宿主负载直接放行等于绕开审批，直接拒绝则等于替运维做决定，两者都越界。
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.types import Command
from xxhash import xxh3_128_hexdigest

from aidev_agent.core.hooks import HOOK_ASK_REASON
from aidev_agent.packages.interrupt_manager.processor import InterruptProcessor
from aidev_agent.packages.interrupt_manager.types import (
    ASK_USER_QUESTION_REASON,
    TOOL_APPROVAL_REASON,
    DispatchResult,
    InterruptOutcome,
    ResumeInputResult,
)

from .errors import RESUME_INTERRUPT_NOT_FOUND, RESUME_INTERRUPT_UNSUPPORTED, HeadlessError

logger = logging.getLogger("aidev-agent.headless")

#: 本期 headless 接线可恢复的中断 reason 白名单
RESUMABLE_REASONS: frozenset[str] = frozenset({HOOK_ASK_REASON, ASK_USER_QUESTION_REASON.value})

#: ITSM 权限审批 reason（不接线）
ITSM_APPROVAL_REASON = TOOL_APPROVAL_REASON.value


def interrupt_id_of(interrupt: Any) -> str:
    """取中断 id（LangGraph ``Interrupt`` 的 ``.id``，兼容 dict 形态）。"""
    intr_id = getattr(interrupt, "id", None)
    if intr_id is None and isinstance(interrupt, dict):
        intr_id = interrupt.get("id") or interrupt.get("interruptId")
    return str(intr_id) if intr_id else ""


def interrupt_reason_of(interrupt: Any) -> str | None:
    """取中断 reason（``value.reason``，兼容 ``interrupt_reason``）。"""
    value = getattr(interrupt, "value", interrupt)
    if not isinstance(value, dict):
        return None
    return value.get("reason") or value.get("interrupt_reason")


def interrupt_tool_call_id_of(interrupt: Any) -> str | None:
    value = getattr(interrupt, "value", interrupt)
    if not isinstance(value, dict):
        return None
    tool_call_id = value.get("toolCallId") or value.get("tool_call_id")
    return str(tool_call_id) if tool_call_id else None


def match_pending_interrupt(pending: list[dict], item: dict) -> dict | None:
    """按 ``interruptId``（携带 ``toolCallId`` 时一并校验）命中 pending 中断。"""
    interrupt_id = item.get("interruptId")
    if not isinstance(interrupt_id, str) or not interrupt_id:
        return None
    tool_call_id = item.get("toolCallId")
    for candidate in pending:
        if str(candidate.get("id")) != interrupt_id:
            continue
        if tool_call_id and str(candidate.get("toolCallId") or "") != str(tool_call_id):
            continue
        return candidate
    return None


def resolve_resume_value(item: dict) -> Any:
    """把恢复负载的 ``decision`` 归一为传给 ``interrupt()`` 的恢复值。

    - ``decision`` 为对象（``aidev:user_question`` 的 ``{"answers": [...]}``）→ 原样透传；
    - ``decision`` 为字符串（``allow`` / ``deny``）→ 包成 ``{"decision", "reason"}``，
      让 hook ``ask`` 的 wrapper 能读到拒绝原因；
    - 缺裁决或取值未知 → 按 ``deny`` 处理并记 warning（宁可不执行，也不误放行）。
    """
    decision = item.get("decision")
    reason = item.get("reason")
    if isinstance(decision, dict):
        return decision
    if isinstance(decision, str) and decision.strip().lower() in {"allow", "deny"}:
        payload: dict[str, Any] = {"decision": decision.strip().lower()}
        if reason:
            payload["reason"] = reason
        return payload
    logger.warning(
        "[headless-interrupt] 恢复裁决缺失或取值未知，按 deny 处理：interruptId=%s, decision_type=%s",
        item.get("interruptId"),
        type(decision).__name__,
    )
    return {"decision": "deny"}


class HeadlessInterruptProcessor(InterruptProcessor):
    """headless 中断编排实现：裁决来自宿主负载，全程零平台调用。"""

    def __init__(self, *, resume_item: dict | None = None) -> None:
        # 不注册任何 handler：既不需要 prepare（建单），也不需要 on_resume（DB 门禁）
        super().__init__(handlers={})
        self.resume_item = resume_item or {}

    # ---------------- 流结束侧 ----------------

    def dispatch_interrupts(self, tasks: Any, ctx: Any = None, *, terminal_ids: set[str] | None = None) -> DispatchResult:
        """只归一为 ``built``，不 prepare、不建单、不 enrich（REQ-17）。"""
        result = DispatchResult()
        for interrupt in self._collect_interrupts(tasks):
            result.interrupts.append(
                InterruptOutcome(intr=interrupt, reason=interrupt_reason_of(interrupt), status="built")
            )
        return result

    # ---------------- 续流侧 ----------------

    def get_resume_input(
        self,
        *,
        tasks: Any,
        session_code: str = "",
        thread_id: str = "",
        chat_history: list[Any] | None = None,
    ) -> ResumeInputResult:
        """校验负载命中 pending，构造 ``Command(resume=...)``；不查 DB、不建单。"""
        pending = self._collect_interrupts(tasks)
        target_id = str(self.resume_item.get("interruptId") or "")
        matched = next((intr for intr in pending if interrupt_id_of(intr) == target_id), None)
        if matched is None:
            logger.warning("[headless-interrupt] resume 未命中 pending：interruptId=%s", target_id)
            return ResumeInputResult(ready=False, next_interrupt=pending[0] if pending else None)

        reason = interrupt_reason_of(matched)
        if reason == ITSM_APPROVAL_REASON:
            raise HeadlessError(
                RESUME_INTERRUPT_UNSUPPORTED,
                "headless 本期不接线 ITSM 权限审批中断的恢复，请由平台侧完成审批",
                {"reason": reason},
            )
        if reason not in RESUMABLE_REASONS:
            raise HeadlessError(
                RESUME_INTERRUPT_NOT_FOUND,
                "该中断类型不在 headless 可恢复范围内",
                {"reason": reason},
            )

        value = resolve_resume_value(self.resume_item)
        pending_tasks = [task for task in tasks or [] if getattr(task, "interrupts", None)]
        if len(pending_tasks) <= 1:
            # 单 pending：直接传取值（与平台单中断裸值语义一致）
            return ResumeInputResult(ready=True, command=Command(resume=value))
        # 多 pending：只指向命中的那个 task（其余保持挂起，方案 D-14）
        owner = next((task for task in pending_tasks if matched in (getattr(task, "interrupts", None) or [])), None)
        if owner is None:  # pragma: no cover - 与上面命中的 pending 同源，理论不可达
            return ResumeInputResult(ready=False, next_interrupt=matched)
        key = xxh3_128_hexdigest(f"{owner.name}:{owner.id}".encode())
        return ResumeInputResult(ready=True, command=Command(resume={key: value}))


__all__ = [
    "HeadlessInterruptProcessor",
    "ITSM_APPROVAL_REASON",
    "RESUMABLE_REASONS",
    "interrupt_id_of",
    "interrupt_reason_of",
    "interrupt_tool_call_id_of",
    "match_pending_interrupt",
    "resolve_resume_value",
]
