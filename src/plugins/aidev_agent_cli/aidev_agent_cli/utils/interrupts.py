# -*- coding: utf-8 -*-
"""pending 中断的纯数据匹配：runtime 做跨轮校验、sdk 做本轮恢复都用同一条规则（只依赖标准库）。"""

from __future__ import annotations


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


__all__ = ["match_pending_interrupt"]
