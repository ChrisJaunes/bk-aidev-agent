# -*- coding: utf-8 -*-
"""headless 用例的本机 fake 模型端点（离线，仅回环地址）。

同时实现两个接口——缺一不可：
- ``POST {base}/chat/completions``：OpenAI 兼容（流式 / 非流式）；
- ``POST {base}/api/token_check``：``ChatModel.get_num_tokens`` 的远端计数接口。

``requests`` 保存收到的请求体，供「历史是否进入模型输入」这类断言使用。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class _FakeHandler(BaseHTTPRequestHandler):
    server_version = "FakeLLM/1.0"

    def log_message(self, *args):  # 静默，避免污染测试输出
        return

    # ---------------- 响应工具 ----------------

    def _json(self, payload: dict, status: int = 200) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _stream(self, body: dict[str, Any], text: str) -> None:
        owner: FakeLLMServer = self.server.owner  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        def chunk(delta: dict, finish: str | None = None) -> None:
            payload = {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": body.get("model", "fake-model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            }
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        chunk({"role": "assistant", "content": ""})
        for piece in owner.chunks(text):
            chunk({"content": piece})
            if owner.chunk_delay:
                time.sleep(owner.chunk_delay)
        chunk({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    # ---------------- 路由 ----------------

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {}

        owner: FakeLLMServer = self.server.owner  # type: ignore[attr-defined]

        if self.path.endswith("/api/token_check"):
            prompts = body.get("prompts") or []
            self._json({"prompts": [{"tokenCount": 8} for _ in prompts]})
            return

        if self.path.endswith("/chat/completions"):
            owner.requests.append(body)
            if owner.error_status is not None:
                # 固定错误响应：模拟模型端点 5xx / 不可达上游
                self._json(
                    {
                        "error": {
                            "message": owner.error_message,
                            "type": "server_error",
                            "code": owner.error_status,
                        }
                    },
                    status=owner.error_status,
                )
                return
            text = owner.next_reply()
            if body.get("stream"):
                with owner.concurrency_lock:
                    owner.in_flight += 1
                    owner.max_concurrency = max(owner.max_concurrency, owner.in_flight)
                try:
                    self._stream(body, text)
                finally:
                    with owner.concurrency_lock:
                        owner.in_flight -= 1
            else:
                self._json(
                    {
                        "id": "chatcmpl-fake",
                        "object": "chat.completion",
                        "created": int(time.time()),
                        "model": body.get("model", "fake-model"),
                        "choices": [
                            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                        ],
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
                    }
                )
            return

        self._json({"error": "not found"}, status=404)


class FakeLLMServer:
    """回环地址上的 fake OpenAI 兼容端点。"""

    def __init__(
        self,
        replies: list[str] | None = None,
        *,
        chunk_delay: float = 0.0,
        error_status: int | None = None,
        error_message: str = "upstream model is unavailable",
    ):
        self._replies = list(replies or ["你好"])
        self._cursor = 0
        self._lock = threading.Lock()
        self.requests: list[dict] = []
        self.chunk_delay = chunk_delay
        self.error_status = error_status
        self.error_message = error_message
        self.concurrency_lock = threading.Lock()
        self.in_flight = 0
        self.max_concurrency = 0
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeHandler)
        self._server.owner = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # ---------------- 生命周期 ----------------

    def start(self) -> "FakeLLMServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def __enter__(self) -> "FakeLLMServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ---------------- 数据 ----------------

    @property
    def base_url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def next_reply(self) -> str:
        with self._lock:
            if not self._replies:
                return "你好"
            reply = self._replies[min(self._cursor, len(self._replies) - 1)]
            self._cursor += 1
            return reply

    @staticmethod
    def chunks(text: str, size: int = 2) -> list[str]:
        return [text[i : i + size] for i in range(0, len(text), size)] or [""]
