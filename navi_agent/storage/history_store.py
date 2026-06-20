from __future__ import annotations

import json
import re
import sqlite3
import threading
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
        defer_persist: bool = False,
    ):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = self.db_path
        self._lock = threading.Lock()
        self._conn = None  # 延迟初始化

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
        self._persist_deferred = defer_persist

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
    def for_querying(cls, db_path: str | Path) -> "HistoryStore":
        """只读构造：仅打开数据库连接，不创建新 session。"""
        instance = cls.__new__(cls)
        instance.db_path = Path(db_path)
        instance.path = instance.db_path
        instance.session_id = ""
        instance.messages = []
        instance.meta = {}
        instance._lock = __import__("threading").Lock()
        instance._conn = None
        instance._init_db()
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

        if getattr(self, "_persist_deferred", False):
            self._persist_deferred = False
            self._upsert_session()   # create the session row first (opens its own connection)

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
                # FTS5 同步由触发器自动完成，无需手动写入
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

    def fork_for_user(
        self,
        messages: list[dict[str, Any]],
        *,
        title: str | None = None,
    ) -> "HistoryStore":
        """Fork for user (parent_session_id=None, not for compression)"""
        child = HistoryStore(
            db_path=self.db_path,
            project_path=self.meta.get("project_path"),
            provider=self.meta.get("provider", ""),
            model=self.meta.get("model", ""),
            parent_session_id=None,
        )
        child.meta["title"] = title or f"Fork of {self.session_id[:12]}..."
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
                self._init_fts_tables(conn)
                self._migrate_fts(conn)

    def _init_fts_tables(self, conn: sqlite3.Connection) -> None:
        """初始化 FTS5 虚拟表和触发器。"""
        # unicode61 FTS5 表
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content_text,
                session_id UNINDEXED,
                role UNINDEXED,
                title UNINDEXED,
                project_path UNINDEXED,
                tokenize='unicode61'
            )
        """)

        # trigram FTS5 表（CJK 子串搜索）
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
                content_text,
                session_id UNINDEXED,
                role UNINDEXED,
                title UNINDEXED,
                project_path UNINDEXED,
                tokenize='trigram'
            )
        """)

        # 触发器：自动同步 FTS5 表
        triggers = [
            ("messages_fts_insert", "INSERT", "messages_fts"),
            ("messages_fts_update", "UPDATE", "messages_fts"),
            ("messages_fts_delete", "DELETE", "messages_fts"),
            ("messages_fts_trigram_insert", "INSERT", "messages_fts_trigram"),
            ("messages_fts_trigram_update", "UPDATE", "messages_fts_trigram"),
            ("messages_fts_trigram_delete", "DELETE", "messages_fts_trigram"),
        ]

        for trigger_name, event, fts_table in triggers:
            if event == "INSERT":
                sql = f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER INSERT ON messages BEGIN
                        INSERT INTO {fts_table}(rowid, content_text, session_id, role, title, project_path)
                        VALUES (new.id, new.content_text, new.session_id, new.role, '', '');
                    END
                """
            elif event == "UPDATE":
                sql = f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER UPDATE ON messages BEGIN
                        DELETE FROM {fts_table} WHERE rowid = old.id;
                        INSERT INTO {fts_table}(rowid, content_text, session_id, role, title, project_path)
                        VALUES (new.id, new.content_text, new.session_id, new.role, '', '');
                    END
                """
            else:  # DELETE
                sql = f"""
                    CREATE TRIGGER IF NOT EXISTS {trigger_name}
                    AFTER DELETE ON messages BEGIN
                        DELETE FROM {fts_table} WHERE rowid = old.id;
                    END
                """
            conn.execute(sql)

    def _migrate_fts(self, conn: sqlite3.Connection) -> None:
        """将已有数据补录到 trigram FTS5 表（一次性迁移）。"""
        fts_count = conn.execute("SELECT count(*) FROM messages_fts_trigram").fetchone()[0]
        msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
        if fts_count == 0 and msg_count > 0:
            conn.execute("""
                INSERT INTO messages_fts_trigram(rowid, content_text, session_id, role, title, project_path)
                SELECT id, content_text, session_id, role, '', '' FROM messages
            """)

    def _upsert_session(self, conn: sqlite3.Connection | None = None) -> None:
        if getattr(self, "_persist_deferred", False):
            return
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

    # ── 搜索相关方法 ──────────────────────────────────────────────────

    def search_messages(
        self,
        query: str,
        limit: int = 20,
        offset: int = 0,
        sort: str = None,
        session_id: str = None,
        role_filter: list[str] = None,
    ) -> list[dict[str, Any]]:
        """
        FTS5 全文搜索，返回匹配消息 + ±1 上下文。

        支持 FTS5 查询语法：
          - 简单关键词: "docker deployment"
          - 短语: '"exact phrase"'
          - 布尔: "docker OR kubernetes", "python NOT java"
          - 前缀: "deploy*"

        sort 控制排序：
          - None (默认): FTS5 BM25 相关性
          - "newest": 按时间降序
          - "oldest": 按时间升序
        """
        if not query or not query.strip():
            return []

        query = self._sanitize_fts5_query(query)
        if not query:
            return []

        # 规范化 sort
        if isinstance(sort, str):
            sort_norm = sort.strip().lower()
            if sort_norm not in ("newest", "oldest"):
                sort_norm = None
        else:
            sort_norm = None

        # ORDER BY
        if sort_norm == "newest":
            order_by_sql = "ORDER BY m.created_at DESC, rank"
        elif sort_norm == "oldest":
            order_by_sql = "ORDER BY m.created_at ASC, rank"
        else:
            order_by_sql = "ORDER BY rank"

        # 构建 WHERE 子句
        where_clauses = ["messages_fts MATCH ?"]
        params: list = [query]

        if session_id:
            where_clauses.append("m.session_id = ?")
            params.append(session_id)

        if role_filter:
            role_placeholders = ",".join("?" for _ in role_filter)
            where_clauses.append(f"m.role IN ({role_placeholders})")
            params.extend(role_filter)

        where_sql = " AND ".join(where_clauses)
        params.extend([limit, offset])

        sql = f"""
            SELECT
                m.id,
                m.session_id,
                m.role,
                snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet,
                m.content_text,
                m.created_at,
                s.title,
                s.project_path,
                s.model
            FROM messages_fts
            JOIN messages m ON m.id = messages_fts.rowid
            JOIN sessions s ON s.session_id = m.session_id
            WHERE {where_sql}
            {order_by_sql}
            LIMIT ? OFFSET ?
        """

        # CJK 查询处理
        is_cjk = self._contains_cjk(query)
        with self._connect() as conn:
            if is_cjk:
                raw_query = query.strip('"').strip()
                cjk_count = self._count_cjk(raw_query)

                # 检查每个 token 的 CJK 长度
                _tokens_for_check = [
                    t for t in raw_query.split()
                    if t.upper() not in {"AND", "OR", "NOT"} and self._contains_cjk(t)
                ]
                _any_short_cjk = any(
                    self._count_cjk(t) < 3 for t in _tokens_for_check
                )

                if cjk_count >= 3 and not _any_short_cjk:
                    # trigram FTS5 路径
                    tokens = raw_query.split()
                    parts = []
                    for tok in tokens:
                        if tok.upper() in {"AND", "OR", "NOT"}:
                            parts.append(tok)
                        else:
                            parts.append('"' + tok.replace('"', '""') + '"')
                    trigram_query = " ".join(parts)

                    tri_where = ["messages_fts_trigram MATCH ?"]
                    tri_params: list = [trigram_query]
                    if session_id:
                        tri_where.append("m.session_id = ?")
                        tri_params.append(session_id)
                    if role_filter:
                        tri_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                        tri_params.extend(role_filter)

                    tri_sql = f"""
                        SELECT
                            m.id,
                            m.session_id,
                            m.role,
                            snippet(messages_fts_trigram, 0, '>>>', '<<<', '...', 40) AS snippet,
                            m.content_text,
                            m.created_at,
                            s.title,
                            s.project_path,
                            s.model
                        FROM messages_fts_trigram
                        JOIN messages m ON m.id = messages_fts_trigram.rowid
                        JOIN sessions s ON s.session_id = m.session_id
                        WHERE {' AND '.join(tri_where)}
                        {order_by_sql}
                        LIMIT ? OFFSET ?
                    """
                    tri_params.extend([limit, offset])
                    try:
                        tri_cursor = conn.execute(tri_sql, tri_params)
                    except sqlite3.OperationalError:
                        matches = []
                    else:
                        matches = [dict(row) for row in tri_cursor.fetchall()]
                else:
                    # LIKE 回退路径（短 CJK 查询）
                    non_op_tokens = [
                        t for t in raw_query.split()
                        if t.upper() not in {"AND", "OR", "NOT"}
                    ] or [raw_query]
                    token_clauses = []
                    like_params: list = []
                    for tok in non_op_tokens:
                        esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                        token_clauses.append(
                            "(m.content_text LIKE ? ESCAPE '\\')"
                        )
                        like_params.append(f"%{esc}%")
                    like_where = [f"({' OR '.join(token_clauses)})"]
                    if session_id:
                        like_where.append("m.session_id = ?")
                        like_params.append(session_id)
                    if role_filter:
                        like_where.append(f"m.role IN ({','.join('?' for _ in role_filter)})")
                        like_params.extend(role_filter)

                    like_sql = f"""
                        SELECT m.id, m.session_id, m.role,
                               substr(m.content_text,
                                      max(1, instr(m.content_text, ?) - 40),
                                      120) AS snippet,
                               m.content_text, m.created_at,
                               s.title, s.project_path, s.model
                        FROM messages m
                        JOIN sessions s ON s.session_id = m.session_id
                        WHERE {' AND '.join(like_where)}
                        ORDER BY m.created_at DESC
                        LIMIT ? OFFSET ?
                    """
                    like_params = [non_op_tokens[0]] + like_params
                    like_params.extend([limit, offset])
                    like_cursor = conn.execute(like_sql, like_params)
                    matches = [dict(row) for row in like_cursor.fetchall()]
            else:
                # 标准 FTS5 路径
                try:
                    cursor = conn.execute(sql, params)
                except sqlite3.OperationalError:
                    return []
                else:
                    matches = [dict(row) for row in cursor.fetchall()]

        # 添加 ±1 上下文
        for match in matches:
            try:
                match["context"] = self._get_context_messages(match["id"], window=1)
            except Exception:
                match["context"] = []

        # 移除完整 content_text（snippet 足够）
        for match in matches:
            match.pop("content_text", None)

        return matches

    def get_messages_around(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
    ) -> dict[str, Any]:
        """
        获取锚定消息 ± window 条上下文。

        返回：
          - window: 消息列表
          - messages_before: 锚点之前的消息数
          - messages_after: 锚点之后的消息数
        """
        if window < 0:
            window = 0
        with self._connect() as conn:
            # 确认锚点存在
            anchor_exists = conn.execute(
                "SELECT 1 FROM messages WHERE id = ? AND session_id = ? LIMIT 1",
                (around_message_id, session_id),
            ).fetchone()
            if not anchor_exists:
                return {"window": [], "messages_before": 0, "messages_after": 0}

            # 锚点 + 前面的消息
            before_rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id <= ? "
                "ORDER BY id DESC LIMIT ?",
                (session_id, around_message_id, window + 1),
            ).fetchall()
            # 锚点后面的消息
            after_rows = conn.execute(
                "SELECT * FROM messages "
                "WHERE session_id = ? AND id > ? "
                "ORDER BY id ASC LIMIT ?",
                (session_id, around_message_id, window),
            ).fetchall()

        # 合并结果
        rows = list(reversed(before_rows)) + list(after_rows)
        result = []
        for row in rows:
            msg = dict(row)
            # 解析 raw_json 获取完整消息
            try:
                raw = json.loads(msg.get("raw_json", "{}"))
                msg["content"] = raw.get("content", msg.get("content_text", ""))
            except (json.JSONDecodeError, TypeError):
                msg["content"] = msg.get("content_text", "")
            result.append(msg)

        messages_before = max(0, len(before_rows) - 1)
        messages_after = len(after_rows)
        return {
            "window": result,
            "messages_before": messages_before,
            "messages_after": messages_after,
        }

    def _get_context_messages(self, message_id: int, window: int = 1) -> list[dict[str, str]]:
        """获取消息的 ±window 条上下文。"""
        with self._connect() as conn:
            ctx_cursor = conn.execute(
                """WITH target AS (
                       SELECT session_id, id
                       FROM messages
                       WHERE id = ?
                   )
                   SELECT role, content_text
                   FROM (
                       SELECT m.id, m.role, m.content_text
                       FROM messages m
                       JOIN target t ON t.session_id = m.session_id
                       WHERE m.id < (SELECT id FROM target)
                       ORDER BY m.id DESC
                       LIMIT ?
                   )
                   UNION ALL
                   SELECT role, content_text
                   FROM messages
                   WHERE id = ?
                   UNION ALL
                   SELECT role, content_text
                   FROM (
                       SELECT m.id, m.role, m.content_text
                       FROM messages m
                       JOIN target t ON t.session_id = m.session_id
                       WHERE m.id > (SELECT id FROM target)
                       ORDER BY m.id ASC
                       LIMIT ?
                   )""",
                (message_id, window, message_id, window),
            )
            context_msgs = []
            for r in ctx_cursor.fetchall():
                content = r["content_text"] or ""
                context_msgs.append({
                    "role": r["role"],
                    "content": content[:200],
                })
        return context_msgs

    @staticmethod
    def _sanitize_fts5_query(query: str) -> str:
        """清洗 FTS5 查询，处理特殊字符。"""
        if not query:
            return ""

        # 保留引号包裹的短语
        quoted_phrases = []
        def _preserve_quoted(m):
            quoted_phrases.append(m.group(0))
            return f"__QUOTED_{len(quoted_phrases) - 1}__"
        sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)

        # 移除 FTS5 特殊字符
        sanitized = re.sub(r'[+{}()\\"^]', " ", sanitized)

        # 折叠重复的 *
        sanitized = re.sub(r"\*{2,}", "*", sanitized)

        # 移除尾部悬空布尔运算符
        sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())

        # 包裹含 - 或 . 的术语
        def _quote_term(m):
            term = m.group(0)
            if term.upper() in {"AND", "OR", "NOT"}:
                return term
            return f'"{term}"'
        sanitized = re.sub(r"[\w][\w\-\.]*[\w]", _quote_term, sanitized)

        # 恢复引号短语
        for i, phrase in enumerate(quoted_phrases):
            sanitized = sanitized.replace(f"__QUOTED_{i}__", phrase)

        return sanitized.strip()

    @staticmethod
    def _contains_cjk(text: str) -> bool:
        """检测是否包含 CJK 字符。"""
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0x2E80 <= cp <= 0x2EFF or    # CJK Radicals
                0x3000 <= cp <= 0x303F or    # CJK Symbols
                0x3040 <= cp <= 0x309F or    # Hiragana
                0x30A0 <= cp <= 0x30FF or    # Katakana
                0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
                return True
        return False

    @classmethod
    def _count_cjk(cls, text: str) -> int:
        """统计 CJK 字符数。"""
        return sum(1 for ch in text if cls._is_cjk_codepoint(ord(ch)))

    @staticmethod
    def _is_cjk_codepoint(cp: int) -> bool:
        """判断是否是 CJK 码位。"""
        return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
                0x3400 <= cp <= 0x4DBF or    # CJK Extension A
                0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """获取单条 session 元数据。"""
        with self._connect() as conn:
            row = conn.execute(
                """SELECT session_id, title, created_at, updated_at,
                          project_path, provider, model, parent_session_id,
                          last_prompt_tokens, last_completion_tokens, message_count
                   FROM sessions WHERE session_id = ?""",
                (session_id,),
            ).fetchone()
            if not row:
                return None
            return dict(row)

    def get_anchored_view(
        self,
        session_id: str,
        around_message_id: int,
        window: int = 5,
        bookend: int = 3,
    ) -> dict[str, Any]:
        """返回锚定窗口 + session 首尾 bookend。

        三层切片：
          - window: 锚点 ± window 条消息
          - bookend_start: session 开头的前 bookend 条 user+assistant 消息（与 window 不重叠）
          - bookend_end: session 末尾的后 bookend 条 user+assistant 消息（与 window 不重叠）
        """
        if bookend < 0:
            bookend = 0

        primitive = self.get_messages_around(session_id, around_message_id, window=window)
        window_rows = primitive["window"]
        empty = {
            "window": [],
            "messages_before": 0,
            "messages_after": 0,
            "bookend_start": [],
            "bookend_end": [],
        }
        if not window_rows:
            return empty

        window_min_id = window_rows[0]["id"]
        window_max_id = window_rows[-1]["id"]

        bookend_start_rows: list[dict] = []
        bookend_end_rows: list[dict] = []

        if bookend > 0:
            with self._connect() as conn:
                # bookend_start: session 开头、window 之前、非空 content
                bookend_start_rows = [
                    dict(r) for r in conn.execute(
                        """SELECT id, role, content_text
                           FROM messages
                           WHERE session_id = ? AND id < ?
                             AND role IN ('user', 'assistant')
                             AND content_text IS NOT NULL AND content_text != ''
                           ORDER BY id ASC
                           LIMIT ?""",
                        (session_id, window_min_id, bookend),
                    ).fetchall()
                ]

                # bookend_end: session 末尾、window 之后、非空 content
                bookend_end_rows = [
                    dict(r) for r in conn.execute(
                        """SELECT id, role, content_text
                           FROM messages
                           WHERE session_id = ? AND id > ?
                             AND role IN ('user', 'assistant')
                             AND content_text IS NOT NULL AND content_text != ''
                           ORDER BY id DESC
                           LIMIT ?""",
                        (session_id, window_max_id, bookend),
                    ).fetchall()
                ]
                bookend_end_rows.reverse()

        # 格式化 bookend 消息
        def _fmt_bookend(row: dict) -> dict:
            content = row.get("content_text") or ""
            return {"id": row["id"], "role": row["role"], "content": content[:200]}

        return {
            "window": window_rows,
            "messages_before": primitive.get("messages_before", 0),
            "messages_after": primitive.get("messages_after", 0),
            "bookend_start": [_fmt_bookend(r) for r in bookend_start_rows],
            "bookend_end": [_fmt_bookend(r) for r in bookend_end_rows],
        }

    def list_sessions_rich(
        self,
        limit: int = 20,
        order_by_last_active: bool = True,
    ) -> list[dict[str, Any]]:
        """列出最近会话，带 preview（第一条 user 消息的前 60 字符）。

        排除有 parent_session_id 的子会话（压缩产生的子会话）。
        """
        order_col = "s.updated_at" if order_by_last_active else "s.created_at"
        with self._connect() as conn:
            rows = conn.execute(
                f"""SELECT s.session_id, s.title, s.created_at, s.updated_at,
                           s.message_count, s.model, s.parent_session_id,
                           (SELECT substr(m.content_text, 1, 60)
                            FROM messages m
                            WHERE m.session_id = s.session_id AND m.role = 'user'
                            ORDER BY m.seq ASC LIMIT 1) AS preview
                    FROM sessions s
                    WHERE s.parent_session_id IS NULL
                    ORDER BY {order_col} DESC
                    LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
