import asyncio
import tempfile
from io import StringIO
from pathlib import Path

from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from navi_agent.tools.approval import ApprovalAction, ApprovalDecision, RiskLevel
import navi_agent.cli.main as cli
from navi_agent.cli.prompt_ui import NaviPromptSession


def _make_prompt(pipe_input, **kwargs):
    return NaviPromptSession(
        history_path=Path(tempfile.gettempdir()) / "navi_prompt_test_history.txt",
        completer=None,
        key_bindings=KeyBindings(),
        bottom_toolbar=lambda: [("", "toolbar")],
        input=pipe_input,
        output=DummyOutput(),
        **kwargs,
    )


def test_prompt_approval_panel_renders_decision():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "echo hi"},
            approval_key="run:echo hi",
            command="echo hi",
        )

        prompt.show_approval(decision)
        text = fragment_list_to_text(prompt._render_approval())

    assert "Need approval" in text
    assert "run_command" in text
    assert "echo hi" in text


def test_prompt_clear_approval_removes_panel():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "echo hi"},
            approval_key="run:echo hi",
            command="echo hi",
        )

        prompt.show_approval(decision)
        prompt.clear_approval()
        text = fragment_list_to_text(prompt._render_approval())

        assert text == ""


def test_prompt_box_in_above_input():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        text = fragment_list_to_text(prompt._render_box_top())
    assert "╭" in text

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        text = fragment_list_to_text(prompt._render_box_bottom())
    assert "╰" in text
    assert "╯" in text


def test_prompt_input_window_config():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        window = _find_buffer_window(prompt)

    assert window is not None
    assert bool(window.dont_extend_height()) is True


def test_running_prompt_enter_inserts_newline():
    """Running-mode UI should no longer advertise Enter as queue."""
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)

        prompt.begin_running()
        text = fragment_list_to_text(prompt._render_toolbar())

        assert "enter: newline" in text
        assert "enter: queue" not in text


def test_running_toolbar_shows_interrupt_requested():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        prompt.begin_running()

        initial_text = fragment_list_to_text(prompt._render_toolbar())
        assert "ctrl+c: interrupt" in initial_text

        prompt._cancel_requested = True
        interrupted_text = fragment_list_to_text(prompt._render_toolbar())

        assert "interrupt requested" in interrupted_text
        assert "waiting for current operation" in interrupted_text


def test_running_ctrl_c_requests_cancel_without_exiting_app():
    cancels = []

    class FakeApp:
        def __init__(self):
            self.exited = False
            self.invalidated = False

        def exit(self, result=None):
            self.exited = True

        def invalidate(self):
            self.invalidated = True

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_cancel=lambda: cancels.append(True))
        prompt.begin_running()
        event = FakeEvent()

        prompt._handle_ctrl_c(event)

        assert cancels == [True]
        assert prompt.cancel_requested is True
        assert event.app.invalidated is True
        assert event.app.exited is False


def test_running_double_ctrl_c_marks_force_exit_without_exiting_app():
    cancels = []

    class FakeApp:
        def __init__(self):
            self.exited = False

        def exit(self, result=None):
            self.exited = True

        def invalidate(self):
            pass

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_cancel=lambda: cancels.append(True))
        prompt.begin_running()
        event = FakeEvent()

        prompt._handle_ctrl_c(event)
        prompt._handle_ctrl_c(event)

        assert cancels == [True, True]
        assert prompt.force_exit is True
        assert event.app.exited is False


def test_prompt_approval_sends_choice_callback():
    choices = []
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=choices.append)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "echo hi"},
            approval_key="run:echo hi",
            command="echo hi",
        )

        prompt.show_approval(decision)
        prompt._submit_approval(1)  # "Allow for this session"

        assert [choice.value for choice in choices] == ["allow_session"]
        assert fragment_list_to_text(prompt._render_approval()) == ""


def test_prompt_approval_cancel_does_not_send_reject():
    choices = []
    cancels = []
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(
            pipe_input,
            on_approval_response=choices.append,
            on_cancel=lambda: cancels.append(True),
        )
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "echo hi"},
            approval_key="run:echo hi",
            command="echo hi",
        )

        prompt.show_approval(decision)
        prompt._cancel_approval()

        assert choices == []
        assert cancels == [True]
        assert prompt.cancel_requested is True
        assert fragment_list_to_text(prompt._render_approval()) == ""


def test_splash_matches_kimi_style_welcome_card(monkeypatch):
    output = StringIO()
    test_console = Console(file=output, force_terminal=False, width=100, highlight=False)
    monkeypatch.setattr(cli, "console", test_console)

    cli.print_splash(
        Path("E:/light_agent/navi_agent"),
        "mimo-v2.5-pro",
        "normal",
        "session_test",
    )

    text = output.getvalue()
    assert "Welcome to Navi!" in text
    assert "███╗" in text
    assert "Run /help to get started." in text
    assert "Directory:" in text
    assert "Session:" in text
    assert "Model:" in text
    assert "Version:" in text
    assert "Commands" not in text
    assert "Press" not in text


def _find_buffer_window(prompt):
    target_buffer = prompt._buffer

    def walk(container):
        content = getattr(container, "content", None)
        if isinstance(content, BufferControl) and content.buffer is target_buffer:
            return container

        children = getattr(container, "children", None)
        if children is not None:
            items = children() if callable(children) else children
            for child in (items or []):
                found = walk(child)
                if found is not None:
                    return found

        if content is not None and content is not container:
            return walk(content)
        return None

    return walk(prompt._layout.container)
