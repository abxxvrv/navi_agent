from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class HistoryStore:
    def __init__(
        self,
        db_path: str | Path,
        project_path: str | None = None,
        provider: str = "",
        model: str = "",
        parent_session_id: str | None = None,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = self.db_path

        self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
        created_at = self._now()
        self.messages: list[dict[str, Any]] = []
        self.meta = {
            "session_id": self.session_id,
            "title": "Untitled session",
            "created_at": created_at,
            "updated_at": created_at,
            "project_path": str(Path(project_path).resolve()) if project_path else str(Path.cwd().resolve()),
            "provider": provider,
            "model": model,
            "tool_names": [],
            "parent_session_id": parent_session_id,
        }

        self._init_db()
        self._upsert_session()

    @classmethod
    def from_existing(cls, db_path: str | Path, session_id: str) -> "HistoryStore":
        instance = cls.__new__(cls)
        instance.db_path = Path(db_path)
        instance.path = instance.db_path
        instance.session_id = session_id
        instance._init_db()

        with instance._connect() as conn:
            row = conn.execute(
                """
                SELECT session_id, title, created_at, updated_at, project_path,
                       provider, model, tool_names_json, last_prompt_tokens,
                       last_completion_tokens, parent_session_id
                FROM sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

            if row is None:
                raise FileNotFoundError(f"Session not found in history database: {session_id}")

            usage = {}
            if row["last_prompt_tokens"] is not None:
                usage["prompt_tokens"] = row["last_prompt_tokens"]
            if row["last_completion_tokens"] is not None:
                usage["completion_tokens"] = row["last_completion_tokens"]

            instance.meta = {
                "session_id": row["session_id"],
                "title": row["title"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "project_path": row["project_path"],
                "provider": row["provider"],
                "model": row["model"],
                "tool_names": instance._decode_tool_names(row["tool_names_json"]),
                "parent_session_id": row["parent_session_id"],
            }
            if usage:
                instance.meta["last_usage"] = usage

            message_rows = conn.execute(
                """
                SELECT raw_json
                FROM messages
                WHERE session_id = ?
                ORDER BY seq ASC
                """,
                (session_id,),
            ).fetchall()

        instance.messages = []
        for message_row in message_rows:
            try:
                message = json.loads(message_row["raw_json"])
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                instance.messages.append(message)

        return instance

    @classmethod
    def list_sessions(cls, db_path: str | Path, limit: int = 20) -> list[dict[str, Any]]:
        path = Path(db_path)
        if not path.is_file():
            return []

        store = cls.__new__(cls)
        store.db_path = path
        store.path = path
        store._init_db()

        with store._connect() as conn:
            rows = conn.execute(
                """
                SELECT session_id, title, project_path, created_at, updated_at,
                       parent_session_id
                FROM sessions
                ORDER BY updated_at DESC, created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [
            {
                "session_id": row["session_id"],
                "title": row["title"],
                "project_path": row["project_path"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "parent_session_id": row["parent_session_id"],
            }
            for row in rows
        ]

    @classmethod
    def latest_session_id(cls, db_path: str | Path) -> str | None:
        sessions = cls.list_sessions(db_path, limit=1)
        return sessions[0]["session_id"] if sessions else None

    def append_message(self, message: dict[str, Any]) -> None:
        record = {key: value for key, value in message.items() if value is not None}
        self.messages.append(record)

        now = self._now()
        self.meta["updated_at"] = now
        if record.get("role") == "user" and self.meta.get("title") == "Untitled session":
            self.meta["title"] = self._make_title(str(record.get("content", "")))

        seq = len(self.messages) - 1
        raw_json = json.dumps(record, ensure_ascii=False)
        content_text = self._content_text(record)

        with self._connect() as conn:
            with conn:
                conn.execute(
                    """
                    INSERT INTO messages (
                        session_id, seq, role, content_text, name,
                        tool_call_id, raw_json, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        self.session_id,
                        seq,
                        str(record.get("role", "")),
                        content_text,
                        record.get("name"),
                        record.get("tool_call_id"),
                        raw_json,
                        now,
                    ),
                )
                message_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO messages_fts (
                        rowid, content_text, session_id, role, title, project_path
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        content_text,
                        self.session_id,
                        str(record.get("role", "")),
                        self.meta.get("title", ""),
                        self.meta.get("project_path", ""),
                    ),
                )
                self._upsert_session(conn)

    def save_usage(self, usage: dict[str, int]) -> None:
        self.meta["last_usage"] = {
            key: value
            for key, value in usage.items()
            if key in ("prompt_tokens", "completion_tokens")
        }
        self._upsert_session()

    def get_usage(self) -> dict[str, int]:
        raw = self.meta.get("last_usage", {})
        return {k: v for k, v in raw.items() if k in ("prompt_tokens", "completion_tokens")}

    def fork_with_messages(
        self,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
    ) -> "HistoryStore":
        child = HistoryStore(
            db_path=self.db_path,
            project_path=self.meta.get("project_path"),
            provider=self.meta.get("provider", ""),
            model=self.meta.get("model", ""),
            parent_session_id=self.session_id,
        )
        child.meta["title"] = title or self.meta.get("title", "Untitled session")
        child.meta["tool_names"] = list(self.meta.get("tool_names", []))
        usage = self.get_usage()
        if usage:
            child.meta["last_usage"] = usage
        child._upsert_session()

        for message in messages:
            child.append_message(message)

        return child

    def set_tool_names(self, tool_names: list[str]) -> None:
        seen: set[str] = set()
        self.meta["tool_names"] = [
            name
            for name in tool_names
            if name and not (name in seen or seen.add(name))
        ]
        self._upsert_session()

    def set_model(self, provider: str, model: str) -> None:
        self.meta["provider"] = provider
        self.meta["model"] = model
        self.meta["updated_at"] = self._now()
        self._upsert_session()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            with conn:
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL DEFAULT 'Untitled session',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        project_path TEXT NOT NULL DEFAULT '',
                        provider TEXT NOT NULL DEFAULT '',
                        model TEXT NOT NULL DEFAULT '',
                        tool_names_json TEXT NOT NULL DEFAULT '[]',
                        parent_session_id TEXT,
                        last_prompt_tokens INTEGER,
                        last_completion_tokens INTEGER,
                        message_count INTEGER NOT NULL DEFAULT 0,
                        archived INTEGER NOT NULL DEFAULT 0
                    );

                    CREATE TABLE IF NOT EXISTS messages (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        session_id TEXT NOT NULL,
                        seq INTEGER NOT NULL,
                        role TEXT NOT NULL,
                        content_text TEXT NOT NULL DEFAULT '',
                        name TEXT,
                        tool_call_id TEXT,
                        raw_json TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE,
                        UNIQUE(session_id, seq)
                    );

                    CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                        content_text,
                        session_id UNINDEXED,
                        role UNINDEXED,
                        title UNINDEXED,
                        project_path UNINDEXED,
                        tokenize='unicode61'
                    );

                    CREATE INDEX IF NOT EXISTS idx_sessions_updated_at
                        ON sessions(updated_at DESC);
                    CREATE INDEX IF NOT EXISTS idx_sessions_project_path
                        ON sessions(project_path);
                    CREATE INDEX IF NOT EXISTS idx_messages_session_seq
                        ON messages(session_id, seq);
                    CREATE INDEX IF NOT EXISTS idx_messages_role
                        ON messages(role);
                    """
                )
                self._ensure_column(conn, "sessions", "parent_session_id", "TEXT")

    def _upsert_session(self, conn: sqlite3.Connection | None = None) -> None:
        usage = self.get_usage()
        params = (
            self.session_id,
            self.meta.get("title", "Untitled session"),
            self.meta.get("created_at", ""),
            self.meta.get("updated_at", self._now()),
            self.meta.get("project_path", ""),
            self.meta.get("provider", ""),
            self.meta.get("model", ""),
            json.dumps(self.meta.get("tool_names", []), ensure_ascii=False),
            self.meta.get("parent_session_id"),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            len(self.messages),
        )

        sql = """
            INSERT INTO sessions (
                session_id, title, created_at, updated_at, project_path,
                provider, model, tool_names_json, parent_session_id,
                last_prompt_tokens, last_completion_tokens, message_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                title = excluded.title,
                updated_at = excluded.updated_at,
                project_path = excluded.project_path,
                provider = excluded.provider,
                model = excluded.model,
                tool_names_json = excluded.tool_names_json,
                parent_session_id = excluded.parent_session_id,
                last_prompt_tokens = excluded.last_prompt_tokens,
                last_completion_tokens = excluded.last_completion_tokens,
                message_count = excluded.message_count
        """

        if conn is not None:
            conn.execute(sql, params)
            return

        with self._connect() as owned_conn:
            with owned_conn:
                owned_conn.execute(sql, params)

    def _ensure_column(
        self,
        conn: sqlite3.Connection,
        table: str,
        column: str,
        definition: str,
    ) -> None:
        columns = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _decode_tool_names(self, value: str | None) -> list[str]:
        if not value:
            return []
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(raw, list):
            return []
        return [str(item) for item in raw if item]

    def _content_text(self, message: dict[str, Any]) -> str:
        content = message.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        parts.append(text)
            return "\n".join(parts)
        return json.dumps(content, ensure_ascii=False)

    def _make_title(self, user: str) -> str:
        title = " ".join(user.strip().split())
        return title[:40] if title else "Untitled session"

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")
