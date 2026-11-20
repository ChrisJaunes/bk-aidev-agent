"""Durable local run facts. Connections are owned by the calling worker."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..infrastructure.logging import BKAI_HOME_DIRNAME


class LocalHistory:
    def __init__(self) -> None:
        override = os.environ.get("BKAI_TUI_HISTORY_FILE")
        path = Path(override) if override else Path.home() / BKAI_HOME_DIRNAME / "history.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_sessions (session_code TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_runs (run_id TEXT PRIMARY KEY, session_code TEXT NOT NULL, "
            "status TEXT NOT NULL, forced INTEGER NOT NULL DEFAULT 0)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS runtime_stages (run_id TEXT PRIMARY KEY, session_code TEXT NOT NULL, state TEXT NOT NULL)"
        )
        self.connection.commit()

    def load(self, session_code: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT state FROM runtime_sessions WHERE session_code = ?", (session_code,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, session_code: str, state: dict[str, Any]) -> None:
        data = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_sessions VALUES (?, ?) "
                "ON CONFLICT(session_code) DO UPDATE SET state=excluded.state",
                (session_code, data),
            )

    def stage(self, run_id: str, session_code: str, state: dict[str, Any]) -> None:
        data = json.dumps(state, ensure_ascii=False, allow_nan=False)
        with self.connection:
            self.connection.execute("INSERT OR REPLACE INTO runtime_stages VALUES (?, ?, ?)", (run_id, session_code, data))

    def finalize(self, run_id: str, *, commit: bool) -> None:
        with self.connection:
            if commit:
                self.connection.execute(
                    "INSERT INTO runtime_sessions SELECT session_code, state FROM runtime_stages WHERE run_id = ? "
                    "ON CONFLICT(session_code) DO UPDATE SET state=excluded.state", (run_id,)
                )
            self.connection.execute("DELETE FROM runtime_stages WHERE run_id = ?", (run_id,))

    def record_run(self, run_id: str, session_code: str, status: str, *, forced: bool = False) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_runs VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "status=excluded.status, forced=excluded.forced",
                (run_id, session_code, status, int(forced)),
            )

    def close(self) -> None:
        self.connection.close()
