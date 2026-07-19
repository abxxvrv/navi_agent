import queue
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from navi_agent.tools.approval import ApprovalManager, UserApprovalChoice
from navi_agent.runtime.interrupt import clear_all, is_interrupted, set_interrupt
from navi_agent.runtime.interrupt_scope import TurnScope
from navi_agent.runtime.interruptible import wait_approval
from navi_agent.model.request import ModelStreamRunner
from navi_agent.runtime.agent import AgentRuntime
from navi_agent.runtime.task_manager import TaskManager
from navi_agent.runtime.tool_context import CURRENT_TOOL_CONTEXT
from navi_agent.storage.agent_store import AgentInstanceStore
from navi_agent.tools.builtin import RunCommandTool
from navi_agent.tools.registry import ToolRegistry


class _FakeRegistry:
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, _name, _args):
        return self.fn()


class _FakeContextManager:
    def _read_text_file(self, _path, default=None):
        return default

    def _render_system_prompt_template(self, template, **_kwargs):
        return template

    def load_agents_md(self):
        return ""

    def build_skill_index_prompt(self):
        return ""


def _make_subagent_runtime(tmp_path):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.workspace = Path(tmp_path)
    runtime.tool_registry = ToolRegistry()
    runtime.tool_registry.register(
        "bash",
        "",
        {"type": "object"},
        lambda **_kwargs: {"ok": True},
    )
    runtime.context_manager = _FakeContextManager()
    runtime.plugin_agents = {}
    runtime.hooks = SimpleNamespace(dispatch=lambda *_args, **_kwargs: None)
    runtime.navi_home = Path(tmp_path)
    runtime._system_prompt = "system"
    runtime.router = SimpleNamespace(provider="fake", model="fake", _provider=object())
    runtime.max_steps = 20
    runtime.session_store = SimpleNamespace(session_id="session-parent")
    runtime.agent_store = AgentInstanceStore(Path(tmp_path) / "agents")
    runtime.task_manager = TaskManager(Path(tmp_path) / "tasks")
    runtime.approval_manager = ApprovalManager(
        mode="strict",
        workspace=Path(tmp_path),
        navi_home=Path(tmp_path),
    )
    runtime._emit = lambda _event: None
    runtime.cancel_event = threading.Event()
    runtime._current_scope = TurnScope(runtime.cancel_event)
    runtime._current_scope.reset()
    runtime._current_scope.attach_execution_thread()
    runtime._approval_lock = threading.Lock()
    return runtime


def test_runtime_interrupt_reaches_registered_tool_worker():
    clear_all()
    entered = threading.Event()
    result_queue = queue.Queue()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.event_handler = None
    runtime.cancel_event = threading.Event()
    runtime._execution_thread_id = None
    runtime._tool_worker_threads = set()
    runtime._tool_worker_threads_lock = threading.Lock()
    runtime.session_store = SimpleNamespace(session_id="session")
    runtime.hooks = SimpleNamespace(dispatch=lambda *_args, **_kwargs: None)

    def tool_fn():
        entered.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if is_interrupted():
                return {"ok": False, "interrupted": True}
            time.sleep(0.01)
        return {"ok": True, "interrupted": False}

    runtime.tool_registry = _FakeRegistry(tool_fn)

    def run_tool():
        result_queue.put(runtime._execute_single_tool("fake", {}, "call_1"))

    thread = threading.Thread(target=run_tool)
    thread.start()
    assert entered.wait(timeout=1)

    runtime.interrupt()
    thread.join(timeout=2)

    call_id, result, tool_name, _args = result_queue.get_nowait()
    assert call_id == "call_1"
    assert tool_name == "fake"
    assert result["interrupted"] is True

    clear_all()


def test_runtime_interrupt_uses_current_turn_scope_for_tool_worker():
    clear_all()
    entered = threading.Event()
    result_queue = queue.Queue()

    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.event_handler = None
    runtime.cancel_event = threading.Event()
    runtime._execution_thread_id = None
    runtime._tool_worker_threads = set()
    runtime._tool_worker_threads_lock = threading.Lock()
    runtime.session_store = SimpleNamespace(session_id="session")
    runtime.hooks = SimpleNamespace(dispatch=lambda *_args, **_kwargs: None)
    runtime._current_scope = TurnScope(runtime.cancel_event)
    runtime._current_scope.attach_execution_thread()

    def tool_fn():
        entered.set()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            if is_interrupted():
                return {"ok": False, "interrupted": True}
            time.sleep(0.01)
        return {"ok": True, "interrupted": False}

    runtime.tool_registry = _FakeRegistry(tool_fn)

    def run_tool():
        result_queue.put(runtime._execute_single_tool("fake", {}, "call_1"))

    thread = threading.Thread(target=run_tool)
    thread.start()
    assert entered.wait(timeout=1)

    runtime.interrupt()
    thread.join(timeout=2)

    call_id, result, tool_name, _args = result_queue.get_nowait()
    assert call_id == "call_1"
    assert tool_name == "fake"
    assert result["interrupted"] is True
    assert runtime.cancel_event.is_set()

    runtime._current_scope.close()
    clear_all()


def test_wait_approval_registers_handler_canceller():
    scope = TurnScope(threading.Event())
    entered = threading.Event()
    cancelled = threading.Event()
    result = {}

    def approval_handler(_decision):
        entered.set()
        cancelled.wait(timeout=2)
        return UserApprovalChoice.REJECT

    def cancel_current():
        cancelled.set()

    approval_handler.cancel_current = cancel_current

    def wait_for_choice():
        try:
            wait_approval(scope, approval_handler, object())
        except KeyboardInterrupt as exc:
            result["error"] = exc

    thread = threading.Thread(target=wait_for_choice)
    thread.start()
    assert entered.wait(timeout=1)

    scope.cancel()
    thread.join(timeout=1)

    assert cancelled.is_set()
    assert isinstance(result["error"], KeyboardInterrupt)


def test_runtime_interrupt_aborts_active_model_request():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.cancel_event = threading.Event()
    runtime._execution_thread_id = None
    runtime._tool_worker_threads = set()
    runtime._tool_worker_threads_lock = threading.Lock()

    class FakeModelRunner:
        def __init__(self):
            self.aborted = False

        def abort(self):
            self.aborted = True

    runner = FakeModelRunner()
    runtime._model_stream_runner = runner

    runtime.interrupt()

    assert runtime.cancel_event.is_set()
    assert runner.aborted is True


def test_agent_loop_stops_when_interrupted_after_tool_node():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.max_steps = 4
    runtime.cancel_event = threading.Event()
    runtime._current_scope = None
    runtime._llm_node = lambda messages: [
        *messages,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1"}],
        },
    ]

    def interrupting_tool_node(messages):
        runtime.cancel_event.set()
        return [*messages, {"role": "tool", "tool_call_id": "call-1", "content": "{}"}]

    runtime._tool_node = interrupting_tool_node

    with pytest.raises(KeyboardInterrupt):
        runtime._run_agent_loop([{"role": "tool", "content": "{}"}])


def test_agent_loop_stops_when_interrupted_after_final_model_response():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.max_steps = 1
    runtime.cancel_event = threading.Event()
    runtime._current_scope = None

    def interrupting_llm_node(messages):
        runtime.cancel_event.set()
        return [*messages, {"role": "assistant", "content": "done"}]

    runtime._llm_node = interrupting_llm_node

    with pytest.raises(KeyboardInterrupt):
        runtime._run_agent_loop([{"role": "user", "content": "hello"}])


def test_agent_loop_enforces_max_steps_before_tool_execution():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.max_steps = 1
    runtime.cancel_event = threading.Event()
    runtime._current_scope = None
    runtime._llm_node = lambda messages: [
        *messages,
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call-1"}],
        },
    ]
    runtime._tool_node = lambda messages: pytest.fail("tool node should not run")

    with pytest.raises(RuntimeError, match="最大执行步数（1）"):
        runtime._run_agent_loop([{"role": "user", "content": "hello"}])


def test_model_stream_runner_aborts_blocked_request_worker():
    cancel_event = threading.Event()
    entered = threading.Event()
    result = {}

    class FakeClient:
        def __init__(self):
            self.closed = threading.Event()

        def close(self):
            self.closed.set()

    class FakeRouter:
        def __init__(self):
            self.client = FakeClient()

        def create_request_client(self):
            return self.client

        def chat_stream_with_client(self, client, messages, tools):
            entered.set()
            client.closed.wait(timeout=2)
            return iter(())

    router = FakeRouter()
    runner = ModelStreamRunner(router, cancel_event, poll_interval=0.01)

    def consume():
        try:
            list(runner.stream(messages=[], tools=[]))
        except KeyboardInterrupt as exc:
            result["error"] = exc

    thread = threading.Thread(target=consume)
    thread.start()
    assert entered.wait(timeout=1)

    cancel_event.set()
    thread.join(timeout=1)

    assert isinstance(result["error"], KeyboardInterrupt)
    assert router.client.closed.is_set()


def test_run_command_kills_process_when_interrupted(tmp_path):
    clear_all()
    tool = RunCommandTool(workspace=str(tmp_path))
    tool.shell_path = "bash"

    set_interrupt(True)
    try:
        result = tool("sleep 30", timeout_seconds=30)
    finally:
        set_interrupt(False)
        if tool.task_manager is not None:
            tool.task_manager.shutdown()

    assert result["ok"] is False
    assert result["interrupted"] is True
    assert result["error"] == "命令执行已中断。"

    clear_all()


def test_subagent_background_returns_and_exposes_output_and_tool_context(
    monkeypatch, tmp_path
):
    runtime = _make_subagent_runtime(tmp_path)
    hook_events = []
    runtime.hooks = SimpleNamespace(
        dispatch=lambda event, session_id, payload, scope=None: hook_events.append(
            (event, session_id, payload)
        )
        or None
    )
    entered = threading.Event()
    release = threading.Event()
    seen = {}

    def read_file(path):
        context = CURRENT_TOOL_CONTEXT.get()
        seen["path"] = path
        seen["tool_call_id"] = context.tool_call_id if context else None
        seen["scope"] = context.scope if context else None
        return {"ok": True, "content": "data"}

    runtime.tool_registry.register(
        "read_file",
        "",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        read_file,
    )

    class FakeAgent:
        def __init__(self, executor):
            self.executor = executor

        def run(self, user_input):
            self.executor("child_call_7", "read_file", {"path": "README.md"})
            entered.set()
            release.wait(timeout=5)
            return SimpleNamespace(
                success=True,
                content="done",
                steps=2,
                tool_calls_made=[{"name": "read_file"}],
            )

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent",
        lambda **kwargs: FakeAgent(kwargs["tool_executor"]),
    )

    try:
        result = runtime._run_subagent(
            prompt="inspect the repository",
            description="inspect repository",
            background=True,
        )

        assert result["ok"] is True
        assert result["backgrounded"] is True
        assert result["task_id"] == result["subagent_id"]
        assert result["status"] == "running"
        assert entered.wait(timeout=1)
        assert seen["path"] == "README.md"
        assert seen["tool_call_id"] == "child_call_7"
        assert seen["scope"] is not None
        assert runtime.task_manager.get_output([result["task_id"]])[0]["status"] == "running"

        release.set()
        task = runtime.task_manager.wait_tasks(
            [result["task_id"]], timeout_ms=1_000
        )[0]
        assert task["status"] == "completed"
        assert task["output"] == "done"
        assert task["subagent_type"] == "general-purpose"
        assert task["steps"] == 2
        assert task["tool_calls"] == 1
        assert [event[0] for event in hook_events] == [
            "SubagentStart",
            "PreToolUse",
            "PostToolUse",
            "SubagentStop",
        ]
        assert hook_events[0][1] == "session-parent"
        assert hook_events[1][1] == result["subagent_id"]
        assert hook_events[2][1] == result["subagent_id"]
        assert hook_events[3][1] == "session-parent"
        assert hook_events[0][2]["subagentId"] == result["subagent_id"]
        assert hook_events[3][2]["exitCode"] == 0
    finally:
        release.set()
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_foreground_returns_complete_result(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)

    class FakeAgent:
        def run(self, user_input):
            return SimpleNamespace(
                success=True,
                content=f"done: {user_input}",
                steps=3,
                tool_calls_made=[{"name": "read_file"}],
            )

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent", lambda **_kwargs: FakeAgent()
    )

    try:
        result = runtime._run_subagent(
            prompt="finish now",
            description="finish task",
            background=False,
            timeout_ms=1_000,
        )

        assert result["ok"] is True
        assert result["content"] == "done: finish now"
        assert result["subagent_type"] == "general-purpose"
        assert result["steps"] == 3
        assert result["tool_calls"] == 1
        assert result["resume_from_hint"] == result["subagent_id"]
        assert result["duration_secs"] >= 0
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_foreground_returns_when_moved_to_background(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    result = {}

    class FakeAgent:
        def run(self, user_input):
            entered.set()
            release.wait(timeout=5)
            return SimpleNamespace(
                success=True,
                content="done",
                steps=1,
                tool_calls_made=[],
            )

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent", lambda **_kwargs: FakeAgent()
    )
    worker = threading.Thread(
        target=lambda: result.setdefault(
            "value",
            runtime._run_subagent(
                prompt="keep working",
                description="background me",
                background=False,
                timeout_ms=5_000,
            ),
        )
    )
    worker.start()
    try:
        assert entered.wait(timeout=1)
        assert runtime.task_manager.background_current() is not None
        worker.join(timeout=1)

        assert not worker.is_alive()
        assert result["value"]["ok"] is True
        assert result["value"]["backgrounded"] is True
        assert result["value"]["status"] == "running"

        release.set()
        task = runtime.task_manager.wait_tasks(
            [result["value"]["task_id"]], timeout_ms=1_000
        )[0]
        assert task["status"] == "completed"
    finally:
        release.set()
        worker.join(timeout=1)
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_start_hook_interrupt_still_stops_and_cleans_up(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    events = []
    captured = {}

    def dispatch(event, *_args, **_kwargs):
        events.append(event)
        if event == "SubagentStart":
            raise KeyboardInterrupt("hook cancelled")

    runtime.hooks = SimpleNamespace(dispatch=dispatch)

    class FakeAgent:
        def run(self, _user_input):
            raise AssertionError("agent must not run after a cancelled start hook")

    def prepare(**kwargs):
        captured["scope"] = kwargs["scope"]
        return FakeAgent()

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", prepare)
    try:
        result = runtime._run_subagent(
            prompt="cancel before start",
            description="cancelled start",
            background=False,
            timeout_ms=1_000,
        )

        assert result["ok"] is False
        assert result["status"] == "cancelled"
        assert events == ["SubagentStart", "SubagentStop"]
        assert runtime.agent_store.get_meta(result["subagent_id"])["status"] == "cancelled"
        assert captured["scope"].execution_thread_id is None
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_plugin_subagent_uses_qualified_prompt_and_tool_filter(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    runtime.tool_registry.register(
        "read_file",
        "",
        {"type": "object"},
        lambda **_kwargs: {"ok": True},
    )
    runtime.plugin_agents["bundle:reviewer"] = {
        "description": "Review code",
        "prompt": "Plugin reviewer prompt",
        "tools": ["Read", "Bash"],
        "disallowed_tools": ["Bash"],
        "prompt_mode": "extend",
    }
    captured = {}

    class FakeAgent:
        def run(self, user_input):
            return SimpleNamespace(
                success=True,
                content=user_input,
                steps=1,
                tool_calls_made=[],
            )

    def prepare(**kwargs):
        captured.update(kwargs)
        return FakeAgent()

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", prepare)
    try:
        result = runtime._run_subagent(
            prompt="review this",
            description="review",
            subagent_type="bundle:reviewer",
            background=False,
        )
        assert result["ok"] is True
        assert captured["tool_names"] == ["read_file"]
        assert captured["system_prompt"] == "system\n\nPlugin reviewer prompt"

        runtime.plugin_agents["bundle:reviewer"]["prompt_mode"] = "full"
        runtime._run_subagent(
            prompt="review again",
            description="review",
            subagent_type="bundle:reviewer",
            background=False,
        )
        assert captured["system_prompt"] == "Plugin reviewer prompt"
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_foreground_timeout_moves_to_background(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    captured = {}

    class FakeAgent:
        def __init__(self, scope):
            captured["scope"] = scope

        def run(self, user_input):
            entered.set()
            release.wait(timeout=5)
            return SimpleNamespace(
                success=True,
                content="late result",
                steps=1,
                tool_calls_made=[],
            )

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent",
        lambda **kwargs: FakeAgent(kwargs["scope"]),
    )

    try:
        result = runtime._run_subagent(
            prompt="slow task",
            description="slow task",
            background=False,
            timeout_ms=20,
        )

        assert entered.is_set()
        assert result["ok"] is True
        assert result["backgrounded"] is True
        assert result["status"] == "running"
        assert captured["scope"].cancel_event.is_set() is False

        runtime._current_scope.cancel("parent turn ended")
        set_interrupt(False)
        assert captured["scope"].cancel_event.is_set() is False
        release.set()
        task = runtime.task_manager.wait_tasks(
            [result["task_id"]], timeout_ms=1_000
        )[0]
        assert task["status"] == "completed"
        assert task["output"] == "late result"
    finally:
        release.set()
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_parent_interrupt_cancels_foreground_subagent(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    result = {}

    class FakeAgent:
        def __init__(self, scope):
            self.scope = scope

        def run(self, user_input):
            entered.set()
            self.scope.cancel_event.wait(timeout=5)
            self.scope.raise_if_cancelled()

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent",
        lambda **kwargs: FakeAgent(kwargs["scope"]),
    )

    def run_subagent():
        try:
            result["value"] = runtime._run_subagent(
                prompt="wait",
                description="wait for cancellation",
                background=False,
                timeout_ms=5_000,
            )
        except KeyboardInterrupt as exc:
            result["error"] = exc

    thread = threading.Thread(target=run_subagent)
    thread.start()
    assert entered.wait(timeout=1)

    runtime._current_scope.cancel("用户中断")
    set_interrupt(False)
    thread.join(timeout=1)

    assert isinstance(result.get("error"), KeyboardInterrupt)
    assert "value" not in result

    runtime.task_manager.shutdown()
    runtime._current_scope.close()
    clear_all()


def test_kill_background_subagent(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()

    class FakeAgent:
        def __init__(self, scope):
            self.scope = scope

        def run(self, user_input):
            entered.set()
            self.scope.cancel_event.wait(timeout=5)
            self.scope.raise_if_cancelled()

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent",
        lambda **kwargs: FakeAgent(kwargs["scope"]),
    )

    try:
        result = runtime._run_subagent(
            prompt="wait",
            description="background wait",
            background=True,
        )
        assert entered.wait(timeout=1)

        killed = runtime.task_manager.kill(result["task_id"])
        task = runtime.task_manager.wait_tasks(
            [result["task_id"]], timeout_ms=1_000
        )[0]
        assert killed["outcome"] == "killed"
        assert task["status"] == "cancelled"
        assert runtime.agent_store.get_meta(result["subagent_id"])["status"] == "cancelled"
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_rejects_invalid_inputs(tmp_path):
    runtime = _make_subagent_runtime(tmp_path)

    try:
        assert runtime._run_subagent(prompt="", description="x")["ok"] is False
        assert runtime._run_subagent(prompt="x", description="   ")["ok"] is False
        assert runtime._run_subagent(
            prompt="x", description="x", subagent_type="unknown"
        )["ok"] is False
        assert runtime._run_subagent(
            prompt="x", description="x", timeout_ms=0
        )["ok"] is False
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()


def test_subagent_resume_validates_source_and_copies_context(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    inherited_contexts = []

    class FakeAgent:
        def __init__(self, store, agent_id):
            self.store = store
            self.agent_id = agent_id
            inherited_contexts.append(store.load_context(agent_id))

        def run(self, user_input):
            context = [
                *self.store.load_context(self.agent_id),
                {"role": "user", "content": user_input},
                {"role": "assistant", "content": f"done: {user_input}"},
            ]
            self.store.save_context(self.agent_id, context)
            self.store.update_meta(self.agent_id, status="completed")
            return SimpleNamespace(
                success=True,
                content=f"done: {user_input}",
                steps=1,
                tool_calls_made=[],
            )

    monkeypatch.setattr(
        "navi_agent.runtime.agent.prepare_agent",
        lambda **kwargs: FakeAgent(kwargs["store"], kwargs["agent_id"]),
    )

    try:
        first = runtime._run_subagent(
            prompt="first turn",
            description="first turn",
            background=False,
        )
        source_id = first["subagent_id"]
        source_context = runtime.agent_store.load_context(source_id)

        assert runtime._run_subagent(
            prompt="x", description="x", resume_from="../bad"
        )["ok"] is False
        runtime.agent_store.update_meta(source_id, status="running")
        assert runtime._run_subagent(
            prompt="x", description="x", resume_from=source_id
        )["ok"] is False
        runtime.agent_store.update_meta(
            source_id, status="completed", parent_session_id="other-session"
        )
        assert runtime._run_subagent(
            prompt="x", description="x", resume_from=source_id
        )["ok"] is False
        runtime.agent_store.update_meta(source_id, parent_session_id="session-parent")
        assert runtime._run_subagent(
            prompt="x",
            description="x",
            subagent_type="explore",
            resume_from=source_id,
        )["ok"] is False

        resumed = runtime._run_subagent(
            prompt="second turn",
            description="second turn",
            resume_from=source_id,
            background=False,
        )

        assert resumed["ok"] is True, resumed
        assert resumed["subagent_id"] != source_id
        assert inherited_contexts == [[], source_context]
        resumed_meta = runtime.agent_store.get_meta(resumed["subagent_id"])
        assert resumed_meta["resumed_from"] == source_id
        assert resumed_meta["parent_session_id"] == "session-parent"
        assert resumed_meta["agent_type"] == "general-purpose"
    finally:
        runtime.task_manager.shutdown()
        runtime._current_scope.close()
        clear_all()
