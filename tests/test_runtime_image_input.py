import threading
from pathlib import Path
from types import SimpleNamespace

from navi_agent.runtime.agent import AgentRuntime
from navi_agent.runtime.goal import GoalRunner
from navi_agent.storage.goal_store import GoalStore
from navi_agent.storage.history_store import HistoryStore
from navi_agent.tools.registry import ToolRegistry


class FakeReviewer:
    def __init__(self):
        self.user_message_count = 0


class FakeRouter:
    def __init__(self, multimodal):
        self.provider = "test"
        self.model = "model"
        self.model_name = "model"
        self.config = {
            "providers": {
                "test": {
                    "models": {
                        "model": {"multimodal": multimodal},
                    },
                },
            },
        }


def _runtime(tmp_path, *, multimodal):
    store = HistoryStore(
        db_path=tmp_path / "history.sqlite3",
        project_path=tmp_path,
        provider="test",
        model="model",
    )
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.workspace = tmp_path
    runtime.max_steps = 4
    runtime._turn_lock = threading.Lock()
    runtime.cancel_event = threading.Event()
    runtime._current_scope = None
    runtime._execution_thread_id = None
    runtime._tool_worker_threads = set()
    runtime._tool_worker_threads_lock = threading.Lock()
    runtime._pending_attachments = []
    runtime.session_store = store
    runtime.conversation_history = []
    runtime.reviewer = FakeReviewer()
    runtime.router = FakeRouter(multimodal)
    runtime._system_prompt = "system"
    runtime.enable_goal_mode = True
    runtime.tool_registry = ToolRegistry()
    runtime.goal_runner = GoalRunner(runtime, GoalStore(store.db_path))
    runtime._goal_turn_reminder = ""
    runtime.loop_messages = None

    def fake_llm(messages):
        runtime.loop_messages = messages
        assistant = {"role": "assistant", "content": "ok"}
        runtime.session_store.append_message(assistant)
        return [*messages, assistant]

    runtime._llm_node = fake_llm
    return runtime


def test_multimodal_api_message_has_image_url_but_history_has_no_base64(tmp_path):
    image = tmp_path / "img.png"
    image.write_bytes(b"image-bytes")
    runtime = _runtime(tmp_path, multimodal=True)

    result = runtime.run_turn("describe", image_paths=[image])

    assert result["ok"] is True
    user_content = runtime.loop_messages[-1]["content"]
    assert isinstance(user_content, list)
    assert any(part.get("type") == "image_url" for part in user_content)
    assert "data:image/png;base64," in user_content[1]["image_url"]["url"]

    stored_user = [m for m in runtime.session_store.messages if m.get("role") == "user"][0]
    assert "用户发送了图片：" in stored_user["content"]
    assert "用户文本：" in stored_user["content"]
    assert str(image) in stored_user["content"]
    assert "data:image" not in stored_user["content"]
    assert "base64" not in stored_user["content"]
    assert "[screenshot]" not in stored_user["content"]


def test_non_multimodal_image_becomes_description_text(tmp_path):
    image = tmp_path / "img.jpg"
    image.write_bytes(b"image-bytes")
    runtime = _runtime(tmp_path, multimodal=False)
    runtime._vision_tool = lambda: SimpleNamespace(
        __call__=lambda image_path, prompt=None: {"ok": True, "content": f"description for {image_path}"}
    )

    class FakeVision:
        def __call__(self, image_path, prompt=None):
            return {"ok": True, "content": f"description for {image_path}"}

    runtime._vision_tool = lambda: FakeVision()

    runtime.run_turn("", image_paths=[image])

    user_content = runtime.loop_messages[-1]["content"]
    assert isinstance(user_content, str)
    assert "[Image #1]" in user_content
    assert f"description for {image}" in user_content
    assert "data:image" not in user_content

    stored_user = [m for m in runtime.session_store.messages if m.get("role") == "user"][0]
    assert "[Image #1]" in stored_user["content"]
    assert f"description for {image}" in stored_user["content"]


def test_non_multimodal_image_failure_is_text(tmp_path):
    image = tmp_path / "img.webp"
    image.write_bytes(b"image-bytes")
    runtime = _runtime(tmp_path, multimodal=False)

    class FakeVision:
        def __call__(self, image_path, prompt=None):
            return {"ok": False, "error": "vision missing"}

    runtime._vision_tool = lambda: FakeVision()

    runtime.run_turn("what is this", image_paths=[image])

    user_content = runtime.loop_messages[-1]["content"]
    assert "无法分析图片：vision missing" in user_content

    stored_user = [m for m in runtime.session_store.messages if m.get("role") == "user"][0]
    assert "无法分析图片：vision missing" in stored_user["content"]


def test_goal_reminder_follows_user_input_without_being_persisted(tmp_path):
    runtime = _runtime(tmp_path, multimodal=True)
    runtime.goal_runner.create_goal("finish <safely>", "tests pass")

    runtime.run_turn("start")

    assert runtime.loop_messages[-2] == {"role": "user", "content": "start"}
    assert runtime.loop_messages[-1]["role"] == "user"
    assert runtime.loop_messages[-1]["content"].startswith("<system-reminder>")
    assert "finish &lt;safely&gt;" in runtime.loop_messages[-1]["content"]
    assert all(
        not str(message.get("content", "")).startswith("<system-reminder>")
        for message in runtime.session_store.messages
    )
