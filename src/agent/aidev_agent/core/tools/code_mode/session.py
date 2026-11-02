# -*- coding: utf-8 -*-
"""请求级 code mode 会话：执行序号与重试计数。

会话对象由装配期创建、挂在 ``code_execute`` 工具闭包上，生命周期与一次请求一致：
不落库、不影响其它并发请求（REQ-13）。
"""

from __future__ import annotations

import asyncio
import logging
import uuid

from aidev_agent.config import settings

logger = logging.getLogger(__name__)


class CodeModeSession:
    """记录一次请求内的代码执行次数与连续失败次数。

    重试口径（技术方案决策 8）：首次执行不计入重试，连续失败达到
    ``CODE_MODE_MAX_RETRIES`` 后又失败一次才终止本轮。

    同一会话内的代码执行必须串行（非功能约束）：``lock`` 在事件循环内首次使用时
    惰性创建，避免跨事件循环绑定。
    """

    def __init__(self, *, max_retries: int | None = None) -> None:
        configured = settings.CODE_MODE_MAX_RETRIES if max_retries is None else max_retries
        self._max_retries = max(0, int(configured if configured is not None else 3))
        self._failures = 0
        self._run_seq = 0
        self._lock: asyncio.Lock | None = None

    @property
    def lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    @property
    def max_retries(self) -> int:
        return self._max_retries

    @property
    def retries_used(self) -> int:
        return self._failures

    def next_run_id(self) -> str:
        self._run_seq += 1
        return f"r{self._run_seq:03d}-{uuid.uuid4().hex[:8]}"

    def register_success(self) -> None:
        self._failures = 0

    def register_failure(self) -> bool:
        """记录一次失败；返回 True 表示达到上限，本轮应终止。"""

        self._failures += 1
        if self._failures > self._max_retries:
            logger.warning("[code-mode] 连续失败 %s 次（上限 %s 次重试），本轮终止", self._failures, self._max_retries)
            return True
        return False


__all__ = ["CodeModeSession"]
