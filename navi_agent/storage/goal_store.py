from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class GoalStore:
    """Persist one resumable goal per session in the history database."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create(self, session_id: str, objective: str, max_cycles: int = 20) -> dict[str, Any]:
        goal_id = f"g_{uuid.uuid4().hex[:8]}"
        now = self._now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO goals (
                    goal_id, session_id, objective, status, cycle_count,
                    max_cycles, last_summary, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, 'paused', 0, ?, '', NULL, ?, ?)
                """,
                (goal_id, session_id, objective, max_cycles, now, now),
            )
        return self.get(goal_id)

    def get(self, goal_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        if row is None:
            raise FileNotFoundError(f"Goal not found: {goal_id}")
        return dict(row)

    def latest(self, session_id: str, *, active_only: bool = False) -> dict[str, Any] | None:
        query = "SELECT * FROM goals WHERE session_id = ?"
        if active_only:
            query += " AND status NOT IN ('completed', 'cancelled')"
        query += " ORDER BY updated_at DESC LIMIT 1"
        with self._connect() as conn:
            row = conn.execute(query, (session_id,)).fetchone()
        return dict(row) if row is not None else None

    def update(self, goal_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "session_id",
            "status",
            "cycle_count",
            "last_summary",
            "last_error",
        }
        values = {key: value for key, value in fields.items() if key in allowed}
        values["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE goals SET {assignments} WHERE goal_id = ?",
                (*values.values(), goal_id),
            )
        if cursor.rowcount == 0:
            raise FileNotFoundError(f"Goal not found: {goal_id}")
        return self.get(goal_id)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cycle_count INTEGER NOT NULL DEFAULT 0,
                    max_cycles INTEGER NOT NULL DEFAULT 20,
                    last_summary TEXT NOT NULL DEFAULT '',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_goals_session_updated
                    ON goals(session_id, updated_at DESC);
                """
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")
