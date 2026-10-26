# -*- coding: utf-8 -*-
"""headless hook 集成用 fake 宿主：按请求行自动答复（进程内，走真实分发路径）。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable

HookResponder = Callable[[dict], dict | None]


class FakeHost:
    """轮询 stdout 行缓冲，发现 hook 请求行就回一条答复行。

    ``responder`` 返回完整的答复行 dict（``{"id": ..., "ok": true, ...}``）；
    返回 ``None`` 表示不答复（用于超时用例）。
    """

    def __init__(self, harness, responder: HookResponder, *, poll_interval: float = 0.005) -> None:
        self._harness = harness
        self._responder = responder
        self._poll_interval = poll_interval
        self._handled: set[str] = getattr(harness, "handled_hook_ids", set())
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.requests: list[dict] = []

    def __enter__(self) -> "FakeHost":
        self._thread.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def hook_requests(self) -> list[dict]:
        return [line for line in self._harness.lines() if line.get("type") == "request" and line.get("method") == "hook"]

    def _run(self) -> None:
        while not self._stop.is_set():
            for line in list(self._harness.lines()):
                line_id = line.get("id")
                if line.get("type") != "request" or line.get("method") != "hook":
                    continue
                if line_id in self._handled:
                    continue
                self._handled.add(line_id)
                self.requests.append(line)
                reply = self._responder(line)
                if reply is not None:
                    self._harness.send_raw(json.dumps(reply))
            time.sleep(self._poll_interval)


def allow_all(decision: str = "allow") -> HookResponder:
    """一律返回最宽松裁决（``session_start`` 不注入、``tool_pre=allow``、``tool_post=accept``）。"""

    def _responder(request: dict) -> dict[str, Any]:
        point = (request.get("params") or {}).get("point")
        if point == "session_start":
            return {"id": request["id"], "ok": True, "result": {"context": ""}}
        if point == "tool_pre":
            return {"id": request["id"], "ok": True, "result": {"decision": decision}}
        if point == "tool_post":
            return {"id": request["id"], "ok": True, "result": {"decision": "accept"}}
        return {"id": request["id"], "ok": True, "result": {}}

    return _responder


def by_point(mapping: dict[str, dict]) -> HookResponder:
    """按点位返回固定 ``result``。"""

    def _responder(request: dict) -> dict[str, Any]:
        point = (request.get("params") or {}).get("point")
        return {"id": request["id"], "ok": True, "result": mapping.get(point, {})}

    return _responder


def silent(_request: dict) -> dict | None:
    """永不答复（触发 ``timeout_ms`` 降级）。"""
    return None
