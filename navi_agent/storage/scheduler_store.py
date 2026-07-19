from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


class SchedulerStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scheduled_tasks (
                    session_id TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    interval_seconds INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    recurring INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    next_fire_at REAL NOT NULL,
                    expires_at REAL,
                    PRIMARY KEY (session_id, task_id)
                )
                """
            )

    def load(self, session_id: str) -> list[dict[str, Any]]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT task_id, interval_seconds, prompt, recurring,
                       created_at, next_fire_at, expires_at
                FROM scheduled_tasks
                WHERE session_id = ?
                ORDER BY created_at, task_id
                """,
                (session_id,),
            ).fetchall()
        return [
            {
                "id": row["task_id"],
                "interval_seconds": row["interval_seconds"],
                "prompt": row["prompt"],
                "recurring": bool(row["recurring"]),
                "durable": True,
                "created_at": row["created_at"],
                "next_fire_at": row["next_fire_at"],
                "expires_at": row["expires_at"],
            }
            for row in rows
        ]

    def upsert(self, session_id: str, task: dict[str, Any]) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO scheduled_tasks (
                    session_id, task_id, interval_seconds, prompt, recurring,
                    created_at, next_fire_at, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id, task_id) DO UPDATE SET
                    interval_seconds = excluded.interval_seconds,
                    prompt = excluded.prompt,
                    recurring = excluded.recurring,
                    created_at = excluded.created_at,
                    next_fire_at = excluded.next_fire_at,
                    expires_at = excluded.expires_at
                """,
                (
                    session_id,
                    task["id"],
                    task["interval_seconds"],
                    task["prompt"],
                    int(task["recurring"]),
                    task["created_at"],
                    task["next_fire_at"],
                    task["expires_at"],
                ),
            )

    def delete(self, session_id: str, task_id: str) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "DELETE FROM scheduled_tasks WHERE session_id = ? AND task_id = ?",
                (session_id, task_id),
            )
