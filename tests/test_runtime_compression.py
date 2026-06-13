from navi_agent.runtime.agent import AgentRuntime
from navi_agent.storage.history_store import HistoryStore


class FakeCompressor:
    def __init__(self, compressed_messages):
        self.compressed_messages = compressed_messages

    def compress(self, messages, messages_path=None):
        return self.compressed_messages


def test_compress_current_session_to_new_session_switches_runtime_store(tmp_path):
    parent = HistoryStore(
        db_path=tmp_path / "history.sqlite3",
        project_path=tmp_path,
        provider="mimo",
        model="mimo-v2.5-pro",
    )
    parent.append_message({"role": "system", "content": "system prompt"})
    parent.append_message({"role": "user", "content": "original request"})
    parent.append_message({"role": "assistant", "content": "original answer"})

    compressed_messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "[CONTEXT COMPACTION]\n\nsummary"},
    ]

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.session_store = parent
    runtime.conversation_history = AgentRuntime._valid_messages(parent.messages)
    runtime.compressor = FakeCompressor(compressed_messages)

    result = runtime._compress_current_session_to_new_session(reason="manual")

    assert result["ok"] is True
    assert result["compressed"] is True
    assert result["old_session_id"] == parent.session_id
    assert result["new_session_id"] == runtime.session_store.session_id
    assert runtime.session_store.session_id != parent.session_id
    assert runtime.session_store.meta["parent_session_id"] == parent.session_id
    assert runtime.session_store.messages == compressed_messages
    assert runtime.conversation_history == [
        {"role": "user", "content": "[CONTEXT COMPACTION]\n\nsummary"},
    ]

    loaded_parent = HistoryStore.from_existing(parent.db_path, parent.session_id)
    loaded_child = HistoryStore.from_existing(parent.db_path, runtime.session_store.session_id)

    assert [message["content"] for message in loaded_parent.messages] == [
        "system prompt",
        "original request",
        "original answer",
    ]
    assert loaded_child.meta["parent_session_id"] == parent.session_id
    assert loaded_child.messages == compressed_messages
