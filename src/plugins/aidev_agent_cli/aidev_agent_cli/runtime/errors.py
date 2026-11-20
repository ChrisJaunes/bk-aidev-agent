"""Transport-independent application failures."""

from typing import Any


class RuntimeFault(Exception):
    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)
