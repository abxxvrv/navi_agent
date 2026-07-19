from types import SimpleNamespace
from pathlib import Path

from navi_agent.runtime.agent import AgentRuntime
from navi_agent.runtime.scheduler import Scheduler
from navi_agent.runtime.task_manager import TaskManager
from navi_agent.storage.scheduler_store import SchedulerStore
from navi_agent.storage.history_store import HistoryStore


class FakeCompressor:
    def __init__(self, compressed_messages):
        self.compressed_messages = compressed_messages

    def compress(self, messages, messages_path=None):
        return self.compressed_messages


def test_compress_context_to_new_session_switches_runtime_store(tmp_path):
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
    runtime.goal_runner = SimpleNamespace(rebind=lambda old, new: None)
    runtime.navi_home = tmp_path / "navi"
    runtime.task_manager = TaskManager(
        runtime.navi_home / "sessions" / parent.session_id / "tasks"
    )
    scheduler_store = SchedulerStore(parent.db_path)
    runtime.scheduler = Scheduler(
        parent.session_id,
        scheduler_store,
        lambda _event: None,
        now=lambda: 1000,
    )
    scheduled = runtime.scheduler.create("5m", "check deploy", durable=True)

    try:
        result = runtime.compress_context_to_new_session()

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
        assert scheduler_store.load(parent.session_id) == []
        assert [task["id"] for task in scheduler_store.load(result["new_session_id"])] == [
            scheduled["id"]
        ]

        started = runtime.task_manager.start_command(
            "printf done",
            tmp_path,
            shell_path="/bin/bash",
            background=True,
        )
        finished = runtime.task_manager.get_output(
            [started["task_id"]], timeout_ms=2000
        )[0]
        assert Path(finished["output_file"]).parent == (
            runtime.navi_home / "sessions" / result["new_session_id"] / "tasks"
        )
    finally:
        runtime.scheduler.close()
        runtime.task_manager.shutdown()
