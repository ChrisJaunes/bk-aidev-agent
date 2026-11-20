"""Durable local session state and run facts. The caller decides the database path and owns the connection."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class LocalHistory:
    def __init__(self, path: str | Path) -> None:
        path = Path(path)
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

    def record_run(self, run_id: str, session_code: str, status: str, *, forced: bool = False) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO runtime_runs VALUES (?, ?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET "
                "status=excluded.status, forced=excluded.forced",
                (run_id, session_code, status, int(forced)),
            )

    def close(self) -> None:
        self.connection.close()
