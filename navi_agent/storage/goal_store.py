from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class GoalStore:
    """Persist the current goal and its execution budgets."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create(
        self,
        session_id: str,
        objective: str,
        completion_criterion: str = "",
        *,
        replace: bool = False,
    ) -> dict[str, Any]:
        current = self.current(session_id)
        if current is not None and not replace:
            raise ValueError(
                f"Goal {current['goal_id']} is still {current['status']}; "
                "complete, cancel, or replace it first."
            )

        goal_id = f"g_{uuid.uuid4().hex[:8]}"
        now = self._now()
        with self._connect() as conn:
            if current is not None:
                conn.execute("DELETE FROM goals WHERE goal_id = ?", (current["goal_id"],))
            conn.execute(
                """
                INSERT INTO goals (
                    goal_id, session_id, objective, completion_criterion, status,
                    turns_used, tokens_used, active_elapsed_ms, active_since,
                    turn_budget, token_budget, wall_clock_budget_ms, status_reason,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'active', 0, 0, 0, ?, NULL, NULL, NULL, '', ?, ?)
                """,
                (goal_id, session_id, objective, completion_criterion, time.time(), now, now),
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
        return self._snapshot(dict(row))

    def current(self, session_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM goals
                WHERE session_id = ?
                ORDER BY updated_at DESC, rowid DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._snapshot(dict(row)) if row is not None else None

    def set_status(
        self,
        goal_id: str,
        status: str,
        *,
        reason: str = "",
    ) -> dict[str, Any]:
        if status not in {"active", "paused", "blocked"}:
            raise ValueError(f"Unsupported persisted goal status: {status}")

        goal = self.get(goal_id)
        elapsed_ms = goal["active_elapsed_ms"]
        active_since: float | None = None
        if status == "active":
            active_since = goal["active_since"] or time.time()
        elif goal["status"] == "active":
            elapsed_ms = goal["elapsed_ms"]

        return self._update(
            goal_id,
            status=status,
            status_reason=reason,
            active_elapsed_ms=elapsed_ms,
            active_since=active_since,
        )

    def begin_turn(self, goal_id: str) -> dict[str, Any]:
        goal = self.get(goal_id)
        if goal["status"] != "active":
            return goal
        return self._update(goal_id, turns_used=goal["turns_used"] + 1)

    def record_tokens(self, goal_id: str, tokens: int) -> dict[str, Any]:
        goal = self.get(goal_id)
        return self._update(goal_id, tokens_used=goal["tokens_used"] + max(0, tokens))

    def set_budget(self, goal_id: str, value: int, unit: str) -> dict[str, Any]:
        field = {
            "turns": "turn_budget",
            "tokens": "token_budget",
            "milliseconds": "wall_clock_budget_ms",
        }.get(unit)
        if field is None:
            raise ValueError(f"Unsupported budget unit: {unit}")
        return self._update(goal_id, **{field: value})

    def rebind(self, goal_id: str, session_id: str) -> dict[str, Any]:
        return self._update(goal_id, session_id=session_id)

    def checkpoint_time(self, goal_id: str) -> dict[str, Any]:
        goal = self.get(goal_id)
        if goal["status"] != "active":
            return goal
        return self._update(
            goal_id,
            active_elapsed_ms=goal["elapsed_ms"],
            active_since=time.time(),
        )

    def clear(self, goal_id: str, status: str) -> dict[str, Any]:
        goal = self.get(goal_id)
        if goal["status"] == "active":
            goal["active_elapsed_ms"] = goal["elapsed_ms"]
            goal["active_since"] = None
        goal["status"] = status
        goal["updated_at"] = self._now()
        with self._connect() as conn:
            conn.execute("DELETE FROM goals WHERE goal_id = ?", (goal_id,))
        return goal

    def normalize_interrupted(self, session_id: str) -> dict[str, Any] | None:
        goal = self.current(session_id)
        if goal is None or goal["status"] != "active":
            return goal
        # The last active segment may include process downtime, so discard it.
        return self._update(
            goal["goal_id"],
            status="paused",
            status_reason="paused after session resume",
            active_since=None,
        )

    def _update(self, goal_id: str, **fields: Any) -> dict[str, Any]:
        allowed = {
            "session_id",
            "status",
            "turns_used",
            "tokens_used",
            "active_elapsed_ms",
            "active_since",
            "turn_budget",
            "token_budget",
            "wall_clock_budget_ms",
            "status_reason",
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

    def _snapshot(self, goal: dict[str, Any]) -> dict[str, Any]:
        elapsed_ms = int(goal["active_elapsed_ms"])
        if goal["status"] == "active" and goal["active_since"] is not None:
            elapsed_ms += max(0, int((time.time() - goal["active_since"]) * 1000))
        goal["elapsed_ms"] = elapsed_ms
        return goal

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS goals (
                    goal_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    objective TEXT NOT NULL,
                    completion_criterion TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    turns_used INTEGER NOT NULL DEFAULT 0,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    active_elapsed_ms INTEGER NOT NULL DEFAULT 0,
                    active_since REAL,
                    turn_budget INTEGER,
                    token_budget INTEGER,
                    wall_clock_budget_ms INTEGER,
                    status_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(goals)").fetchall()
            }
            additions = {
                "completion_criterion": "TEXT NOT NULL DEFAULT ''",
                "turns_used": "INTEGER NOT NULL DEFAULT 0",
                "tokens_used": "INTEGER NOT NULL DEFAULT 0",
                "active_elapsed_ms": "INTEGER NOT NULL DEFAULT 0",
                "active_since": "REAL",
                "turn_budget": "INTEGER",
                "token_budget": "INTEGER",
                "wall_clock_budget_ms": "INTEGER",
                "status_reason": "TEXT NOT NULL DEFAULT ''",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE goals ADD COLUMN {name} {declaration}")

            if "cycle_count" in columns:
                conn.execute(
                    "UPDATE goals SET turns_used = cycle_count WHERE turns_used = 0"
                )
                conn.execute(
                    "UPDATE goals SET status = 'paused', active_since = NULL "
                    "WHERE status IN ('running', 'verifying')"
                )
                conn.execute(
                    "DELETE FROM goals WHERE status IN ('completed', 'cancelled')"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_goals_session_updated "
                "ON goals(session_id, updated_at DESC)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="milliseconds")
