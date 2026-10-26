# -*- coding: utf-8 -*-
"""NDJSON over stdio 协议层（REQ-6 / REQ-7）。

三种行模型：
- 请求（stdin，一行一条）：``{"id": "...", "method": "...", "params": {...}}``
- 事件（stdout）：``{"id": "...", "type": "event", "event": {...}}``
- 响应（stdout）：``{"id": "...", "ok": true, "result": {...}}`` 或
  ``{"id": "...", "ok": false, "error": {"code": ..., "message": ..., "detail": {...}}}``

事件行位于该请求的响应行之前，每行都带请求 ``id``；事件对象是 ``ag_ui.core`` 原对象，
经 ``model_dump(mode="json", by_alias=True)`` 序列化，不新增类型、不裁剪字段。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ag_ui.core import BaseEvent

from .errors import (
    PROTOCOL_INVALID_JSON,
    PROTOCOL_INVALID_PARAMS,
    PROTOCOL_UNKNOWN_METHOD,
    HeadlessError,
)

METHOD_CREATE = "create"
METHOD_TURN = "turn"
METHOD_RESUME = "resume"
METHOD_CANCEL = "cancel"
METHOD_CLOSE = "close"

#: agent → host 的 hook 请求行方法名（不在 ``SUPPORTED_METHODS`` 内：它不是宿主请求）
METHOD_HOOK = "hook"

SUPPORTED_METHODS = frozenset(
    {
        METHOD_CREATE,
        METHOD_TURN,
        METHOD_RESUME,
        METHOD_CANCEL,
        METHOD_CLOSE,
    }
)


@dataclass
class Request:
    """一条解析后的请求行。"""

    id: str
    method: str
    params: dict[str, Any] = field(default_factory=dict)


def parse_request(line: str) -> Request:
    """把一行请求文本解析为 :class:`Request`。

    :raises HeadlessError: ``protocol.invalid_json``（非法 JSON）、
        ``protocol.invalid_params``（缺字段或类型不符）、``protocol.unknown_method``
        （方法不在五方法集合内）。错误信息只带字段名，不回显请求体。
    """
    try:
        payload = json.loads(line)
    except (ValueError, TypeError) as exc:
        raise HeadlessError(PROTOCOL_INVALID_JSON, f"请求行不是合法 JSON：{type(exc).__name__}") from exc

    if not isinstance(payload, dict):
        raise HeadlessError(PROTOCOL_INVALID_JSON, "请求行必须是 JSON 对象")

    request_id = payload.get("id")
    if not isinstance(request_id, str) or not request_id:
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "id 缺失或类型不符（期望非空 str）", {"fields": ["id"]})

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "method 缺失或类型不符（期望非空 str）", {"fields": ["method"]})
    if method not in SUPPORTED_METHODS:
        raise HeadlessError(
            PROTOCOL_UNKNOWN_METHOD,
            f"未知方法：{method}",
            {"method": method, "supported": sorted(SUPPORTED_METHODS)},
        )

    params = payload.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, "params 类型不符（期望 object）", {"fields": ["params"]})

    return Request(id=request_id, method=method, params=params)


def require_param(params: dict[str, Any], name: str, expected: type | tuple[type, ...] = str, *, optional: bool = False):
    """取出必填参数并校验类型；缺失或类型不符时抛 ``protocol.invalid_params``。"""
    value = params.get(name)
    if value is None:
        if optional:
            return None
        raise HeadlessError(PROTOCOL_INVALID_PARAMS, f"缺少必填字段：{name}", {"fields": [name]})
    if not isinstance(value, expected):
        raise HeadlessError(
            PROTOCOL_INVALID_PARAMS,
            f"字段 {name} 类型不符（期望 {getattr(expected, '__name__', expected)}）",
            {"fields": [name]},
        )
    return value


def encode_event(request_id: str, event: BaseEvent) -> str:
    """把一条 AG-UI 事件编码为事件行。"""
    return json.dumps(
        {"id": request_id, "type": "event", "event": event.model_dump(mode="json", by_alias=True)},
        ensure_ascii=False,
    )


def encode_hook_request(line_id: str, params: dict[str, Any]) -> str:
    """把一次 hook 请求编码为 agent → host 的请求行（REQ-18）。

    ``params`` 固定四键：``session_code`` / ``turn_id`` / ``point`` / ``payload``；
    ``line_id`` 形如 ``<本轮请求 id>#h<n>``，宿主据此原样回填答复行的 ``id``。
    """
    return json.dumps({"id": line_id, "type": "request", "method": METHOD_HOOK, "params": params}, ensure_ascii=False)


def is_hook_reply(payload: Any) -> bool:
    """判断一行入站 JSON 是否为宿主答复行（``ok`` 在、``method`` 不在，REQ-18）。"""
    return isinstance(payload, dict) and "ok" in payload and "method" not in payload


def encode_result(request_id: str, result: dict[str, Any]) -> str:
    """编码成功响应行。"""
    return json.dumps({"id": request_id, "ok": True, "result": result}, ensure_ascii=False)


def encode_error(request_id: str, error: HeadlessError) -> str:
    """编码失败响应行（不含完整请求体与凭据）。"""
    return json.dumps({"id": request_id, "ok": False, "error": error.to_payload()}, ensure_ascii=False)
