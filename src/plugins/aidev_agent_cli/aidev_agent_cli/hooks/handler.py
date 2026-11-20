# -*- coding: utf-8 -*-
"""进程级 hook handler：由 ``__main__`` 解析 ``--hooks`` 构造，经 ``build_runtime`` 交给 ``RuntimeService``。

裁决来源是协议客户端（宿主）：handler 持有一个后绑定的通道，各协议 adapter 在连接就绪后调用
:meth:`ClientHookHandler.attach` 注入「发一次 hook 请求并等结果」的协程函数。未绑定通道时
直接回 ``timeout``，runtime 按降级路径继续，不会卡住。

本模块只依赖标准库、``hooks.contract`` / ``hooks.models`` 与 ``utils.errors``，不导入任何 SDK。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Awaitable, Callable

from .contract import HookConfig
from .models import CancelSignal, HookReply, HookRequest
from ..utils.errors import HOOK_VALUE_INVALID, HeadlessError

#: 发一次 hook 请求（参数见 :meth:`ClientHookHandler.__call__`）并返回宿主的裁决结果。
HookChannel = Callable[[dict[str, Any]], Awaitable[Any]]


class ClientHookHandler:
    """把每个 hook 点位转成一次对协议客户端的请求；超时与取消在这里收敛为 ``HookReply``。"""

    def __init__(self, config: HookConfig) -> None:
        self.config = config
        self._channel: HookChannel | None = None

    def attach(self, channel: HookChannel) -> None:
        self._channel = channel

    async def __call__(self, request: HookRequest, token: CancelSignal) -> HookReply:
        channel = self._channel
        if channel is None:
            return HookReply("timeout")
        params = {
            "sessionId": request.params.get("session_code"),
            "requestId": request.line_id,
            **request.params,
        }

        async def wait_cancel() -> None:
            while not token.cancelled:
                await asyncio.sleep(0.01)

        task = asyncio.ensure_future(channel(params))
        cancel = asyncio.ensure_future(wait_cancel())
        try:
            done, _ = await asyncio.wait(
                {task, cancel}, timeout=request.timeout_ms / 1000, return_when=asyncio.FIRST_COMPLETED
            )
            if token.cancelled:
                return HookReply("cancelled")
            if task not in done:
                return HookReply("timeout")
            try:
                result = task.result()
            except Exception:
                return HookReply("reply", {"id": request.line_id, "ok": False, "error": {"code": "host_failed"}})
            return HookReply("reply", {"id": request.line_id, "ok": True, "result": result})
        finally:
            task.cancel()
            cancel.cancel()
            await asyncio.gather(task, cancel, return_exceptions=True)


def parse_hooks_option(raw: str | None) -> ClientHookHandler | None:
    """``--hooks`` → handler；省略时 ``None``（完全关闭 hook）。

    取值为 JSON 对象（``{"timeout_ms": ..., "points": [...], "module": ...}``），或 ``@路径`` 指向 JSON 文件。
    带 ``module`` 时裁决由 worker 内加载的 hook 模块给出，本 handler 不会被调用（未绑定通道也无妨）。
    形状非法抛 ``HeadlessError(hook.value_invalid)``。
    """
    if raw is None:
        return None
    text = raw
    if raw.startswith("@"):
        try:
            text = Path(raw[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise HeadlessError(HOOK_VALUE_INVALID, f"无法读取 hooks 配置文件：{exc}") from exc
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise HeadlessError(HOOK_VALUE_INVALID, f"hooks 配置不是合法 JSON：{exc}") from exc
    return ClientHookHandler(HookConfig.parse(value))


__all__ = ["ClientHookHandler", "HookChannel", "parse_hooks_option"]
