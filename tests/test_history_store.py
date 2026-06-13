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