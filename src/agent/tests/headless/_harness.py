# -*- coding: utf-8 -*-
"""headless 进程内测试驱动：复用 ``Dispatcher``，不需要真的起子进程。"""

from __future__ import annotations

import io
import json
import threading
from typing import Any

from aidev_agent.headless.cli import Dispatcher
from aidev_agent.headless.protocol import Request


class Harness:
    """把 NDJSON 请求喂给 ``Dispatcher`` 并收集输出行。"""

    def __init__(self, *, dispatcher: Dispatcher | None = None):
        self.out = io.StringIO()
        self.dispatcher = dispatcher or Dispatcher(stream=self.out)
        self._lock = threading.Lock()

    # ---------------- 发送 ----------------

    def send(self, request_id: str, method: str, **params: Any) -> None:
        self.dispatcher.handle_line(json.dumps({"id": request_id, "method": method, "params": params}))

    def send_obj(self, request_id: str, method: str, params: dict[str, Any]) -> None:
        """进程内发送：``params`` 可含不可 JSON 序列化的对象（``event_sink`` / ``checkpointer``）。"""
        self.dispatcher.handle_request(Request(id=request_id, method=method, params=params))

    def send_raw(self, line: str) -> None:
        self.dispatcher.handle_line(line)

    def wait(self, timeout: float = 30.0) -> None:
        self.dispatcher.join(timeout)

    # ---------------- 读取 ----------------

    def lines(self) -> list[dict]:
        with self._lock:
            raw = self.out.getvalue()
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def events(self, request_id: str) -> list[dict]:
        return [line["event"] for line in self.lines() if line.get("type") == "event" and line.get("id") == request_id]

    def event_types(self, request_id: str) -> list[str]:
        return [event["type"] for event in self.events(request_id)]

    def response(self, request_id: str) -> dict:
        matches = [line for line in self.lines() if line.get("id") == request_id and "ok" in line]
        assert len(matches) == 1, f"请求 {request_id} 的响应数量不是 1：{matches}"
        return matches[0]

    def result(self, request_id: str) -> dict:
        response = self.response(request_id)
        assert response["ok"] is True, response
        return response["result"]

    def error(self, request_id: str) -> dict:
        response = self.response(request_id)
        assert response["ok"] is False, response
        return response["error"]

    def reset(self) -> None:
        with self._lock:
            self.out.seek(0)
            self.out.truncate(0)


class EventSink:
    """自定义 ``event_sink``：内存收集器。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    def __call__(self, event: Any) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [event.type for event in self.events]
