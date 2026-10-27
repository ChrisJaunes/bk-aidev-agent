# -*- coding: utf-8 -*-
"""headless 运行面的错误码与结构化异常（REQ-5 / REQ-10）。

错误码表与 ``specs/agent-hook/plan.md`` 的 D-8 一致；每条错误都必须能落成一行
NDJSON 响应（``{"ok": false, "error": {...}}``）并同时产出 AG-UI ``RUN_ERROR`` 事件，
不得静默回落默认实现或返回空回复。
"""

from __future__ import annotations

from typing import Any

# 协议层
PROTOCOL_INVALID_JSON = "protocol.invalid_json"
PROTOCOL_UNKNOWN_METHOD = "protocol.unknown_method"
PROTOCOL_INVALID_PARAMS = "protocol.invalid_params"

# 会话层
SESSION_NOT_FOUND = "session.not_found"
SESSION_CLOSED = "session.closed"
SESSION_BUSY_CANCELLED = "session.busy_cancelled"

# Agent 层
AGENT_BUILD_FAILED = "agent.build_failed"
AGENT_UNSUPPORTED = "agent.unsupported"
AGENT_INTERNAL_ERROR = "agent.internal_error"

# 注入点层
RESUME_PAYLOAD_MISSING = "resume_payload_missing"
RESUME_PAYLOAD_UNSUPPORTED = "resume_payload_unsupported"

# 平台隔离层
PLATFORM_API_UNSUPPORTED = "platform_api_unsupported"


class HeadlessError(Exception):
    """headless 运行面的结构化错误。

    :param code: 稳定错误码（见模块常量），调用方据此分流，不解析文案
    :param message: 面向调用方的一句话说明，不得包含凭据或完整请求体
    :param detail: 可选的结构化补充（只放字段名、类型名等非敏感信息）
    """

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __repr__(self) -> str:  # pragma: no cover - 便于调试
        return f"HeadlessError(code={self.code!r}, message={self.message!r})"


class HeadlessUnsupportedError(HeadlessError):
    """headless 不支持的平台能力（REQ-8）。

    平台 API 一律显式失败，绝不静默降级为网络请求或空实现。
    """

    def __init__(self, method: str, message: str | None = None):
        self.method = method
        super().__init__(
            code=PLATFORM_API_UNSUPPORTED,
            message=message or f"headless 模式不支持平台 API 调用：{method}",
            detail={"method": method},
        )
