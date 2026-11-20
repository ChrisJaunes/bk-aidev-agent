"""Typed SDK inputs, normalized once at the application boundary."""

from dataclasses import dataclass, field
from typing import Any, Literal

from .errors import RuntimeFault


@dataclass(frozen=True)
class ChatInput:
    text: str
    kind: Literal["chat"] = field(default="chat", init=False)

    def __post_init__(self):
        if not isinstance(self.text, str):
            raise RuntimeFault("input.invalid", "无效输入形状")


@dataclass(frozen=True)
class ContextInput:
    messages: list[dict[str, Any]]
    text: str = ""
    kind: Literal["context"] = field(default="context", init=False)

    def __post_init__(self):
        if (
            not isinstance(self.text, str)
            or not isinstance(self.messages, list)
            or not all(isinstance(item, dict) for item in self.messages)
        ):
            raise RuntimeFault("input.invalid", "无效输入形状")


@dataclass(frozen=True)
class ResumeInterrupt:
    payload: dict[str, Any] | list[dict[str, Any]]
    text: str = ""
    kind: Literal["resume_interrupt"] = field(default="resume_interrupt", init=False)

    def __post_init__(self):
        items = self.payload if isinstance(self.payload, list) else [self.payload]
        if (
            len(items) != 1
            or not isinstance(items[0], dict)
            or not isinstance(items[0].get("interruptId"), str)
            or not items[0]["interruptId"]
        ):
            raise RuntimeFault("resume_payload_unsupported", "一次恢复需要一个含非空 interruptId 的对象")
        if not isinstance(self.text, str):
            raise RuntimeFault("input.invalid", "恢复输入必须是字符串")
