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
    controller.output = TerminalOutput(
        lambda *args, **kwargs: None,
        lambda event, **kwargs: event_calls.append((event, kwargs)),
    )

    controller.handle_runtime_event(
        {"type": "tool_start", "tool_name": "grep", "tool_args": {"query": "needle"}}
    )

    assert stream_box.closed is True
    assert prompt.status == ("grep", "query=needle")
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
    controller.output = TerminalOutput(
        lambda *args, **kwargs: None,
        lambda event, **kwargs: event_calls.append((event, kwargs)),
    )
    event = {
        "type": "tool_result",
        "tool_name": "grep",
        "tool_args": {"query": "needle"},
        "tool_result": {"ok": True},
    }

    controller.handle_runtime_event(event)

    assert event_calls == [(event, {"box": controller.stream_box})]
    assert prompt.cleared == ["grep"]
