import asyncio

from navi_agent.cli.chat_controller import ChatController
from navi_agent.cli.terminal_output import TerminalOutput


def test_sigint_routes_to_prompt_when_running(monkeypatch, tmp_path):
    trace_path = tmp_path / "interrupt_trace.log"
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE", "1")
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE_PATH", str(trace_path))

    previous_calls = []
    routed_calls = []
    installed = {}

    def previous_handler(signum, frame):
        previous_calls.append(signum)

    def fake_getsignal(signum):
        return previous_handler

    def fake_signal(signum, handler):
        installed["handler"] = handler

    monkeypatch.setattr("signal.getsignal", fake_getsignal)
    monkeypatch.setattr("signal.signal", fake_signal)

    class FakePromptSession:
        can_handle_interrupt_signal = True
        is_running = True
        cancel_requested = False
        force_exit = False

        def handle_interrupt_signal(self):
            routed_calls.append(True)

    controller = object.__new__(ChatController)
    controller.prompt_session = FakePromptSession()
    controller.runtime = object()
    controller.loop = None

    restore = controller._install_sigint_trace()
    installed["handler"](2, None)

    assert routed_calls == [True]
    assert previous_calls == []

    restore()


def test_runtime_tool_start_updates_prompt_without_scrollback():
    controller = object.__new__(ChatController)
    event_calls = []

    class Prompt:
        def __init__(self):
            self.status = None

        def set_tool_status(self, name, detail):
            self.status = (name, detail)

        def clear_tool_status(self, name=None):
            self.status = None

    class StreamBox:
        has_output = True

        def __init__(self):
            self.closed = False

        def close_all(self):
            self.closed = True

    prompt = Prompt()
    stream_box = StreamBox()
    controller.prompt_session = prompt
    controller.stream_box = stream_box
    controller.loop = None
    controller.current_tool_call_id = None
    controller.output = TerminalOutput(
        lambda *args, **kwargs: None,
        lambda event, **kwargs: event_calls.append((event, kwargs)),
    )

    controller.handle_runtime_event(
        {
            "type": "tool_start",
            "tool_name": "bash",
            "tool_args": {"command": "sleep 10"},
            "tool_call_id": "call-1",
        }
    )

    assert stream_box.closed is True
    assert prompt.status == ("bash", "command=sleep 10")
    assert controller.current_tool_call_id == "call-1"
    assert event_calls == []


def test_runtime_tool_result_prints_history_and_clears_prompt():
    controller = object.__new__(ChatController)
    event_calls = []

    class Prompt:
        def __init__(self):
            self.cleared = []

        def clear_tool_status(self, name=None):
            self.cleared.append(name)

    prompt = Prompt()
    controller.prompt_session = prompt
    controller.stream_box = object()
    controller.loop = None
    controller.current_tool_call_id = "call-1"
    controller.output = TerminalOutput(
        lambda *args, **kwargs: None,
        lambda event, **kwargs: event_calls.append((event, kwargs)),
    )
    event = {
        "type": "tool_result",
        "tool_name": "bash",
        "tool_args": {"command": "sleep 10"},
        "tool_result": {"ok": True},
        "tool_call_id": "call-1",
    }

    controller.handle_runtime_event(event)

    assert event_calls == [(event, {"box": controller.stream_box})]
    assert prompt.cleared == ["bash"]
    assert controller.current_tool_call_id is None


def test_background_runtime_events_enqueue_once_with_origin():
    controller = object.__new__(ChatController)

    class Prompt:
        def __init__(self):
            self._idle_queue = asyncio.Queue()

    prompt = Prompt()
    controller.prompt_session = prompt
    controller.loop = None
    controller.pending_monitor_events = []
    controller.monitor_notification_queued = False
    controller.output = TerminalOutput(
        lambda *args, **kwargs: None,
        lambda *args, **kwargs: None,
    )

    controller.handle_runtime_event(
        {
            "type": "task_completed",
            "task": {
                "task_id": "task-1",
                "task_type": "command",
                "status": "completed",
                "exit_code": 0,
            },
        }
    )
    controller.handle_runtime_event(
        {
            "type": "task_completed",
            "task": {
                "task_id": "agent-1",
                "task_type": "subagent",
                "description": "inspect code",
                "status": "completed",
            },
        }
    )
    controller.handle_runtime_event(
        {
            "type": "monitor_event",
            "task_id": "monitor-1",
            "description": "watch build",
            "output": "build passed",
        }
    )
    controller.handle_runtime_event(
        {
            "type": "scheduled_prompt",
            "task_id": "schedule-1",
            "human_schedule": "every 5 minutes",
            "prompt": "check deployment",
        }
    )

    queued = [prompt._idle_queue.get_nowait() for _ in range(4)]
    assert prompt._idle_queue.empty()
    assert [origin for _text, _images, origin in queued] == [
        "task:task-1",
        "task:agent-1",
        "monitor",
        "scheduler:schedule-1",
    ]
    assert all(images == [] for _text, images, _origin in queued)
    assert "Background command task-1 finished" in queued[0][0]
    assert "Subagent agent-1 (inspect code) finished" in queued[1][0]
    assert queued[2][0] == ""
    assert controller.pending_monitor_events[0]["output"] == "build passed"
    assert "check deployment" in queued[3][0]


def test_monitor_events_share_one_bounded_queue_entry():
    controller = object.__new__(ChatController)

    class Prompt:
        def __init__(self):
            self._idle_queue = asyncio.Queue()

    controller.prompt_session = Prompt()
    controller.loop = None
    controller.pending_monitor_events = []
    controller.monitor_notification_queued = False

    for index in range(100):
        controller.handle_runtime_event({
            "type": "monitor_event",
            "task_id": "monitor-1",
            "description": "watch build",
            "output": str(index),
        })

    assert controller.prompt_session._idle_queue.qsize() == 1
    assert len(controller.pending_monitor_events) == 50
    assert controller.pending_monitor_events[0]["output"] == "50"


def test_bare_loop_prints_usage_without_starting_a_turn():
    controller = object.__new__(ChatController)
    notices = []
    controller.runtime = object()
    controller.prompt_session = object()
    controller.stream_box = object()
    controller.output = TerminalOutput(
        notices.append,
        lambda *args, **kwargs: None,
    )

    asyncio.run(controller.process_message("/loop"))

    assert notices == ["[yellow]Usage: /loop <interval> <prompt>[/yellow]"]
