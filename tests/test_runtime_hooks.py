import threading
from types import SimpleNamespace
from unittest.mock import Mock

from navi_agent.runtime.agent import AgentRuntime
from navi_agent.runtime.interrupt_scope import TurnScope
from navi_agent.tools.approval import ApprovalAction, ApprovalDecision, RiskLevel


def test_pre_tool_hook_runs_before_approval_and_policy_denial_is_reported():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.session_store = SimpleNamespace(session_id="session")
    runtime._emit = lambda _event: None
    scope = TurnScope(threading.Event())
    runtime.hooks = SimpleNamespace(
        dispatch=lambda event, session_id, payload, scope=None: {
            "decision": "deny",
            "reason": "blocked by hook",
        }
    )
    runtime.approval_manager = SimpleNamespace(
        check_tool_call=lambda *_args: (_ for _ in ()).throw(
            AssertionError("approval ran before hook denial")
        )
    )

    result = runtime._handle_approval("call-1", "bash", {"command": "pwd"}, scope)

    assert result["ok"] is False
    assert result["error"] == "blocked by hook"

    events = []
    runtime.hooks = SimpleNamespace(
        dispatch=lambda event, session_id, payload, scope=None: events.append(
            (event, payload)
        )
        or None
    )
    runtime.approval_manager = SimpleNamespace(
        check_tool_call=lambda *_args: ApprovalDecision(
            ApprovalAction.DENY,
            RiskLevel.HARD_DENY,
            "blocked by policy",
            "bash",
            {"command": "pwd"},
        )
    )

    result = runtime._handle_approval("call-2", "bash", {"command": "pwd"}, scope)

    assert result["ok"] is False
    assert [event[0] for event in events] == ["PreToolUse", "PermissionDenied"]
    assert events[-1][1]["source"] == "policy"


def test_tool_result_dispatches_success_and_failure_hooks():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.event_handler = None
    runtime.cancel_event = threading.Event()
    runtime._current_scope = TurnScope(runtime.cancel_event)
    runtime._tool_worker_threads = set()
    runtime._tool_worker_threads_lock = threading.Lock()
    runtime.session_store = SimpleNamespace(session_id="session")
    events = []
    runtime.hooks = SimpleNamespace(
        dispatch=lambda event, session_id, payload, scope=None: events.append(
            (event, payload)
        )
        or None
    )

    runtime.tool_registry = SimpleNamespace(invoke=Mock(return_value={"ok": True}))
    runtime._execute_single_tool("read_file", {"path": "a"}, "call-1")
    runtime.tool_registry.invoke = Mock(return_value={"ok": False, "error": "bad"})
    runtime._execute_single_tool("read_file", {"path": "b"}, "call-2")
    runtime.tool_registry.invoke = Mock(side_effect=RuntimeError("boom"))
    runtime._execute_single_tool("read_file", {"path": "c"}, "call-3")

    assert [event[0] for event in events] == [
        "PostToolUse",
        "PostToolUseFailure",
        "PostToolUseFailure",
    ]
    assert events[-1][1]["error"] == "boom"


def test_close_dispatches_session_end_after_runtime_workers_stop():
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime._turn_lock = threading.Lock()
    calls = []
    runtime.scheduler = SimpleNamespace(close=lambda: calls.append("scheduler"))
    runtime.task_manager = SimpleNamespace(shutdown=lambda: calls.append("tasks"))
    runtime.session_store = SimpleNamespace(session_id="session")
    runtime.hooks = SimpleNamespace(
        dispatch=lambda event, session_id, payload: calls.append(event) or None
    )

    runtime.close()

    assert calls == ["scheduler", "tasks", "SessionEnd"]
