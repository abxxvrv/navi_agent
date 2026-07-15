import queue
import threading
import time
from pathlib import Path

import pytest

from navi_agent.tools.approval import ApprovalManager, UserApprovalChoice
from navi_agent.runtime.interrupt import clear_all, is_interrupted, set_interrupt
from navi_agent.runtime.interrupt_scope import TurnScope
from navi_agent.runtime.interruptible import wait_approval
from navi_agent.model.request import ModelStreamRunner
from navi_agent.runtime.agent import AgentRuntime
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
    runtime.tool_registry = ToolRegistry()
    runtime.tool_registry.register(
        "bash",
        "",
        {"type": "object"},
        lambda **_kwargs: {"ok": True},
    )
    runtime.context_manager = _FakeContextManager()
    runtime.navi_home = Path(tmp_path)
    runtime._system_prompt = "system"
    runtime.router = object()
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


def test_run_command_kills_process_when_interrupted(monkeypatch, tmp_path):
    clear_all()

    class FakeStdout:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class FakeProc:
        def __init__(self, *_args, **_kwargs):
            self.stdout = FakeStdout()
            self.returncode = None
            self.killed = False

        def poll(self):
            return 1 if self.killed else None

    fake_proc_holder = {}

    def fake_popen(*args, **kwargs):
        proc = FakeProc(*args, **kwargs)
        fake_proc_holder["proc"] = proc
        return proc

    monkeypatch.setattr("navi_agent.tools.builtin.subprocess.Popen", fake_popen)

    tool = RunCommandTool(workspace=str(tmp_path))
    tool.shell_path = "bash"

    def fake_kill(proc):
        proc.killed = True

    monkeypatch.setattr(tool, "_kill_process_tree", fake_kill)

    set_interrupt(True)
    try:
        result = tool("sleep 30", timeout_seconds=30)
    finally:
        set_interrupt(False)

    assert result["ok"] is False
    assert result["interrupted"] is True
    assert result["error"] == "命令执行已中断。"
    assert fake_proc_holder["proc"].killed is True

    clear_all()


def test_subagent_timeout_cancels_pending_approval(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    released = threading.Event()
    cancelled = threading.Event()

    def approval_handler(_decision):
        entered.set()
        released.wait(timeout=5)
        return UserApprovalChoice.REJECT

    def cancel_current():
        cancelled.set()
        released.set()

    approval_handler.cancel_current = cancel_current
    runtime.approval_handler = approval_handler

    class FakeAgent:
        def __init__(self, executor):
            self.executor = executor

        def run(self, user_input):
            self.executor("bash", {"command": "python train.py"})
            return type("Result", (), {"content": "done", "steps": 1, "tool_calls_made": []})()

    def fake_prepare_agent(**kwargs):
        return FakeAgent(kwargs["tool_executor"])

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", fake_prepare_agent)

    result = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="run command",
        timeout_ms=100,
    )

    assert entered.is_set()
    assert cancelled.is_set()
    assert result["timeout"] is True
    assert runtime.cancel_event.is_set() is False

    runtime._current_scope.close()
    clear_all()


def test_subagent_rejects_invalid_action_prompt_and_timeout(tmp_path):
    runtime = _make_subagent_runtime(tmp_path)

    bad_action = runtime._run_subagent(
        action="resume",
        subagent_type="general",
        prompt="x",
        timeout_ms=100,
    )
    empty_prompt = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="",
        timeout_ms=100,
    )
    blank_prompt = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="   ",
        timeout_ms=100,
    )
    bad_timeout = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="x",
        timeout_ms=0,
    )

    assert bad_action == {"ok": False, "error": "未知 action：resume，应为 run。"}
    assert empty_prompt == {"ok": False, "error": "prompt 必须是非空字符串。"}
    assert blank_prompt == {"ok": False, "error": "prompt 必须是非空字符串。"}
    assert bad_timeout == {"ok": False, "error": "timeout_ms 必须是大于等于 1 的整数。"}

    runtime._current_scope.close()
    clear_all()


def test_subagent_success_result_only_returns_content(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)

    class FakeAgent:
        def run(self, user_input):
            return type("Result", (), {"content": "done", "steps": 7, "tool_calls_made": [{"name": "x"}]})()

    def fake_prepare_agent(**_kwargs):
        return FakeAgent()

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", fake_prepare_agent)

    result = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="x",
        timeout_ms=1000,
    )

    assert result == {"ok": True, "content": "done"}

    runtime._current_scope.close()
    clear_all()


def test_subagent_timeout_cancel_survives_parent_scope_close(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    captured = {}

    class FakeAgent:
        def __init__(self, scope):
            self.scope = scope

        def run(self, user_input):
            entered.set()
            release.wait(timeout=5)
            captured["cancelled_after_release"] = self.scope.cancel_event.is_set()
            finished.set()
            if self.scope.cancel_event.is_set():
                raise KeyboardInterrupt("子 agent 超时")
            captured["continued"] = True
            return type("Result", (), {"content": "done", "steps": 1, "tool_calls_made": []})()

    def fake_prepare_agent(**kwargs):
        captured["scope"] = kwargs["scope"]
        return FakeAgent(kwargs["scope"])

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", fake_prepare_agent)

    result = runtime._run_subagent(
        action="run",
        subagent_type="general",
        prompt="block",
        timeout_ms=100,
    )

    assert entered.is_set()
    assert result["timeout"] is True

    runtime._current_scope.close()
    release.set()
    assert finished.wait(timeout=1)
    assert captured["cancelled_after_release"] is True
    assert captured.get("continued") is not True

    clear_all()


def test_parent_interrupt_cancels_subagent_approval(monkeypatch, tmp_path):
    runtime = _make_subagent_runtime(tmp_path)
    entered = threading.Event()
    released = threading.Event()
    cancelled = threading.Event()
    result = {}

    def approval_handler(_decision):
        entered.set()
        released.wait(timeout=5)
        return UserApprovalChoice.REJECT

    def cancel_current():
        cancelled.set()
        released.set()

    approval_handler.cancel_current = cancel_current
    runtime.approval_handler = approval_handler

    class FakeAgent:
        def __init__(self, executor):
            self.executor = executor

        def run(self, user_input):
            self.executor("bash", {"command": "python train.py"})
            return type("Result", (), {"content": "done", "steps": 1, "tool_calls_made": []})()

    def fake_prepare_agent(**kwargs):
        return FakeAgent(kwargs["tool_executor"])

    monkeypatch.setattr("navi_agent.runtime.agent.prepare_agent", fake_prepare_agent)

    def run_subagent():
        try:
            result["value"] = runtime._run_subagent(
                action="run",
                subagent_type="general",
                prompt="run command",
                timeout_ms=5_000,
            )
        except KeyboardInterrupt as exc:
            result["error"] = exc

    thread = threading.Thread(target=run_subagent)
    thread.start()
    assert entered.wait(timeout=1)

    runtime._current_scope.cancel("用户中断")
    thread.join(timeout=1)

    assert cancelled.is_set()
    assert isinstance(result.get("error"), KeyboardInterrupt)
    assert "value" not in result

    runtime._current_scope.close()
    clear_all()
