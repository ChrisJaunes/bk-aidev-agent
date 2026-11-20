"""Cross-turn session state, immutable information and concrete session registry."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

from ..utils.runs import SessionInfo, SessionOptions, SessionState, new_session_code
from ..utils.errors import RuntimeFault
from ..utils.history import LocalHistory


@dataclass
class Session:
    options: SessionOptions
    revision: int = 0
    #: 最近一次提交的跨轮状态（失败 / 取消的轮次不会改动它）。
    state: SessionState = field(default_factory=SessionState)
    closed: bool = False

    @property
    def pending(self) -> list[dict[str, Any]]:
        return self.state.interrupts

    @property
    def session_code(self) -> str:
        return self.options.session_code

    def ensure_open(self) -> None:
        if self.closed:
            raise RuntimeFault("session.closed", "会话已关闭")

    def close(self) -> None:
        self.closed = True

class SessionRegistry:
    def __init__(self, defaults, state_file: str):
        self._defaults = defaults
        self._state_file = state_file
        self._sessions: dict[str, Session] = {}
        self._created: dict[str, str] = {}
        self._latest: str | None = None
        self._closed = False

    def new_session(self, options: SessionOptions | None = None) -> SessionInfo:
        if self._closed:
            raise RuntimeError("registry is closed")
        options = options or SessionOptions(new_session_code())
        if options.session_code in self._sessions:
            raise ValueError("session already registered")
        options = replace(
            options,
            resources=options.resources or self._defaults.resources,
            model=options.model or self._defaults.model,
        )
        history = LocalHistory(self._state_file)
        try:
            state = SessionState.from_dict(history.load(options.session_code))
        finally:
            history.close()
        session = Session(options, state=state)
        self._sessions[options.session_code] = session
        self._created[options.session_code] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._latest = options.session_code
        return self.info(options.session_code)

    def info(self, code: str | None) -> SessionInfo | None:
        session = self.get(code)
        return SessionInfo(code, self._created[code], session.closed, session.revision) if session else None

    def get(self, code: str | None) -> Session | None:
        return self._sessions.get(code) if code else None

    def codes(self) -> list[str]:
        return list(self._sessions)

    def list(self) -> list[dict[str, str]]:
        return [self.info(code).record() for code, session in self._sessions.items() if not session.closed]

    def latest(self) -> Session | None:
        session = self.get(self._latest)
        return session if session is not None and not session.closed else None

    def close_all(self) -> None:
        for session in self._sessions.values():
            session.close()
        self._closed = True