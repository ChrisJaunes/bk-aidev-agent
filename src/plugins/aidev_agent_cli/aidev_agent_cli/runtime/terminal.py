# -*- coding: utf-8 -*-
"""纯 stdlib 的 AG-UI 事件字典构造：parent 侧收尾事件与 worker 事件同一形状。

为什么是字典而不是 SDK 模型
---------------------------
``runtime.runner`` 在 worker 加入（reap）之后，必须由**父进程**补出 ``RUN_ERROR`` /
``RUN_FINISHED`` 终止事件。这些事件最终都以 JSON 载荷（``event_payload``）的形式交给
协议 adapter，而父进程的核心链路（``service`` / ``contracts`` / ``models`` / ``auth`` /
``worker`` / ``runs``）必须是**零 SDK** 的：``ag_ui`` 导入期会 print 到 fd 1（那是协议
通道），``aidev_agent`` 导入期会读 settings——父进程提前拉起它们就是启动次序回归。

因此这里只产出**与 ``ag_ui`` 序列化同形的字典**：

* 键名用线上一侧的别名（``threadId`` / ``runId``，与 ``model_dump(by_alias=True)`` 一致）；
* ``timestamp`` 是墙钟毫秒（与 SDK ``stamp_round_end_event`` 同一算法）；
* 成功终止的 ``outcome`` 是 ``{"type": "success"}``——与
  ``serialize_run_finished_outcome(RunFinishedSuccessOutcome())`` 的输出逐字一致。

形状不靠记忆维持：``tests/runtime/test_terminal_events.py`` 把这里的输出与 SDK 真序列化器
的 ``model_dump(mode="json", by_alias=True, exclude_none=True)`` 逐键比对。

本模块只允许标准库：不得 import ``ag_ui`` / ``aidev_agent`` / ``pydantic``，也不得 import
本包任何会拉起它们的模块。
"""

from __future__ import annotations

import time
from typing import Any

#: 取消轮的 ``runId`` 与错误提示语。两者都是线上契约（前端据此识别取消），与 SDK 的
#: ``aidev_agent.utils.event.RunId`` 取值相同；这里固化成常量，父进程不必为两个字符串
#: 导入 SDK。
CANCELLED_RUN_ID = "cancelled"
CANCELLED_MESSAGE = "用户已取消"

RUN_ERROR = "RUN_ERROR"
RUN_FINISHED = "RUN_FINISHED"


def wall_clock_ms() -> int:
    """当前墙钟，毫秒（与 SDK ``wall_clock_ms`` 同一算法）。"""
    return int(time.time() * 1000)


def success_outcome() -> dict[str, Any]:
    """成功终止事件的 ``outcome``：与 SDK 的 ``RunFinishedSuccessOutcome`` 序列化同形。"""
    return {"type": "success"}


def _stamp(event: dict[str, Any]) -> dict[str, Any]:
    """写入墙钟毫秒时间戳；已带时间戳的不覆盖（与 ``stamp_round_end_event`` 同语义）。"""
    event.setdefault("timestamp", wall_clock_ms())

    return event


def build_cancel_terminal_events(thread_id: str, *, run_id: str = CANCELLED_RUN_ID) -> tuple[dict, dict]:
    """构造取消轮的终止事件对：先 ``RUN_ERROR``（取消提示），再 ``RUN_FINISHED``。"""
    error_event = _stamp({"type": RUN_ERROR, "message": CANCELLED_MESSAGE})
    finished_event = _stamp(
        {"type": RUN_FINISHED, "threadId": thread_id, "runId": run_id, "outcome": success_outcome()}
    )

    return error_event, finished_event


def build_error_event(message: str) -> dict[str, Any]:
    """构造一条内部错误的终止事件。"""
    return _stamp({"type": RUN_ERROR, "message": message})


def build_finished_event(thread_id: str, run_id: str) -> dict[str, Any]:
    """构造一条正常结束的终止事件。"""
    return _stamp({"type": RUN_FINISHED, "threadId": thread_id, "runId": run_id, "outcome": success_outcome()})


__all__ = [
    "CANCELLED_MESSAGE",
    "CANCELLED_RUN_ID",
    "RUN_ERROR",
    "RUN_FINISHED",
    "build_cancel_terminal_events",
    "build_error_event",
    "build_finished_event",
    "success_outcome",
    "wall_clock_ms",
]
