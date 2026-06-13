import sqlite3
import time

from navi_agent.storage.history_store import HistoryStore


def test_history_store_creates_sqlite_schema(tmp_path):
    db_path = tmp_path / "history.sqlite3"

    store = HistoryStore(db_path=db_path, project_path=tmp_path, provider="mimo", model="mimo-v2.5-pro")

    assert db_path.is_file()
    assert store.session_id
    assert store.meta["project_path"] == str(tmp_path.resolve())

    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'virtual')"
            )
        }

    assert "sessions" in tables
    assert "messages" in tables
    assert "messages_fts" in tables


def test_history_store_persists_tool_names_and_messages(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    store = HistoryStore(db_path=db_path, project_path=tmp_path, provider="mimo", model="mimo-v2.5-pro")

    store.set_tool_names(["read_file", "write_file", "read_file"])
    store.append_message({"role": "user", "content": "hello history"})
    store.append_message({"role": "assistant", "content": "stored in sqlite"})
    store.save_usage({"prompt_tokens": 10, "completion_tokens": 5, "ignored": 99})

    loaded = HistoryStore.from_existing(db_path, store.session_id)

    assert loaded.meta["tool_names"] == ["read_file", "write_file"]
    assert loaded.meta["title"] == "hello history"
    assert loaded.get_usage() == {"prompt_tokens": 10, "completion_tokens": 5}
    assert [message["role"] for message in loaded.messages] == ["user", "assistant"]
    assert loaded.messages[1]["content"] == "stored in sqlite"

    with sqlite3.connect(db_path) as conn:
        fts_rows = conn.execute(
            "SELECT content_text FROM messages_fts WHERE messages_fts MATCH ?",
            ("sqlite",),
        ).fetchall()

    assert fts_rows == [("stored in sqlite",)]


def test_history_store_lists_latest_sessions(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    first = HistoryStore(db_path=db_path, project_path=tmp_path, provider="mimo", model="first")
    second = HistoryStore(db_path=db_path, project_path=tmp_path, provider="mimo", model="second")
    first.append_message({"role": "user", "content": "older"})
    time.sleep(1.1)
    second.append_message({"role": "user", "content": "newer"})

    sessions = HistoryStore.list_sessions(db_path, limit=2)

    assert [session["session_id"] for session in sessions] == [
        second.session_id,
        first.session_id,
    ]
    assert HistoryStore.latest_session_id(db_path) == second.session_id


def test_fork_with_messages_creates_child_session(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    parent = HistoryStore(db_path=db_path, project_path=tmp_path, provider="mimo", model="mimo-v2.5-pro")
    parent.set_tool_names(["read_file", "run_command"])
    parent.save_usage({"prompt_tokens": 100, "completion_tokens": 20})
    parent.append_message({"role": "system", "content": "system prompt"})
    parent.append_message({"role": "user", "content": "original request"})
    parent.append_message({"role": "assistant", "content": "original answer"})

    compressed_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "[CONTEXT COMPACTION]\n\nsummary"},
        {"role": "user", "content": "latest request"},
    ]

    child = parent.fork_with_messages(compressed_messages, title=parent.meta["title"])

    assert child.session_id != parent.session_id
    assert child.meta["parent_session_id"] == parent.session_id
    assert child.meta["tool_names"] == ["read_file", "run_command"]
    assert child.get_usage() == {"prompt_tokens": 100, "completion_tokens": 20}
    assert [message["content"] for message in parent.messages] == [
        "system prompt",
        "original request",
        "original answer",
    ]
    assert child.messages == compressed_messages

    loaded_parent = HistoryStore.from_existing(db_path, parent.session_id)
    loaded_child = HistoryStore.from_existing(db_path, child.session_id)

    assert loaded_parent.meta["parent_session_id"] is None
    assert loaded_child.meta["parent_session_id"] == parent.session_id
    assert [message["content"] for message in loaded_parent.messages] == [
        "system prompt",
        "original request",
        "original answer",
    ]
    assert loaded_child.messages == compressed_messages


def test_history_store_migrates_parent_session_id_column(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'Untitled session',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                project_path TEXT NOT NULL DEFAULT '',
                provider TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                tool_names_json TEXT NOT NULL DEFAULT '[]',
                last_prompt_tokens INTEGER,
                last_completion_tokens INTEGER,
                message_count INTEGER NOT NULL DEFAULT 0,
                archived INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO sessions (
                session_id, title, created_at, updated_at, project_path,
                provider, model, tool_names_json, message_count
            )
            VALUES (
                'old_session', 'old title', '2026-01-01T00:00:00',
                '2026-01-01T00:00:00', '.', 'mimo', 'model', '[]', 0
            );
            """
        )

    loaded = HistoryStore.from_existing(db_path, "old_session")

    assert loaded.meta["parent_session_id"] is None
    with sqlite3.connect(db_path) as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(sessions)").fetchall()
        }

    assert "parent_session_id" in columns
