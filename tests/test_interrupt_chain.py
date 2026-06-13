import queue
import threading
import time

import pytest

from navi_agent.tools.approval import UserApprovalChoice
from navi_agent.runtime.interrupt import clear_all, is_interrupted, set_interrupt
from navi_agent.runtime.interrupt_scope import TurnScope
from navi_agent.runtime.interruptible import wait_approval
from navi_agent.model.request import ModelStreamRunner
from navi_agent.runtime.agent import AgentRuntime
from navi_agent.tools.builtin import RunCommandTool


class _FakeRegistry:
    def __init__(self, fn):
        self.fn = fn

    def invoke(self, _name, _args):
        return self.fn()


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


def test_should_continue_stops_when_interrupted_after_tool_node():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.cancel_event = threading.Event()
    runtime.cancel_event.set()

    with pytest.raises(KeyboardInterrupt):
        runtime._should_continue({"messages": [{"role": "tool", "content": "{}"}]})


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
    tool.bash_path = "bash"

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
