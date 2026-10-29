# -*- coding: utf-8 -*-
"""``session_start`` 的上下文注入中间件（REQ-14）。

复用模型节点的模板管道路径（``ModelNodeSettings.extra_template_middlewares``）：
在 system prompt 装配阶段追加文本，因此注入只影响本轮模型可见输入 —— 不改会话
账本里的 user 原文、不改本轮 ``turn_id``、也不进平台落库（REQ-14 边界）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class HookContextMiddleware:
    """把 hook 下发的 ``context`` 追加到 system prompt 尾部。"""

    context: str

    def __call__(self, ctx: Any, next: Callable[[], None]) -> None:
        if self.context:
            slots = getattr(ctx, "prompt_slots", None)
            if slots is not None:
                slots.system = f"{slots.system or ''}\n\n{self.context}"
        next()


__all__ = ["HookContextMiddleware"]
