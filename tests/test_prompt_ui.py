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


def test_prompt_approval_command_preview_limits_chars_and_hides_key():
    long_command = "x" * 4000
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": long_command},
            approval_key="shell:exact:" + long_command,
            command=long_command,
        )

        prompt.show_approval(decision)
        collapsed_lines = prompt._approval_lines()
        collapsed_text = fragment_list_to_text(prompt._render_approval())
        prompt._approval_state["command_expanded"] = True
        expanded_lines = prompt._approval_lines()
        expanded_text = fragment_list_to_text(prompt._render_approval())

    assert "Approval key:" not in collapsed_text
    assert "Approval key:" not in expanded_text
    collapsed_command_text = "\n".join(
        text for _style, text in collapsed_lines
        if text.startswith("  Command:") or text.startswith("         ")
    )
    expanded_command_text = "\n".join(
        text for _style, text in expanded_lines
        if text.startswith("  Command:") or text.startswith("         ")
    )
    assert collapsed_command_text.count("x") == 300
    assert expanded_command_text.count("x") == 3000
    assert any(text.startswith("         ") for _style, text in collapsed_lines)
    assert any(text.startswith("         ") for _style, text in expanded_lines)
    assert ("x" * 301) not in collapsed_text
    assert ("x" * 3001) not in expanded_text
    assert "Command preview: collapsed, showing 300/4000 chars," in collapsed_text
    assert "Ctrl+O expand." in collapsed_text
    assert "Command preview: expanded, showing 3000/4000 chars," in expanded_text
    assert "Ctrl+O collapse." in expanded_text


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
        assert "ctrl+c/esc: interrupt" in initial_text

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


def test_running_ctrl_c_writes_interrupt_trace(monkeypatch, tmp_path):
    trace_path = tmp_path / "interrupt_trace.log"
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE", "1")
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE_PATH", str(trace_path))

    class FakeApp:
        is_running = True

        def exit(self, result=None):
            pass

        def invalidate(self):
            pass

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_cancel=lambda: None)
        prompt.begin_running()

        prompt._handle_ctrl_c(FakeEvent())

    trace_text = trace_path.read_text(encoding="utf-8")
    assert '"source": "prompt_toolkit_ctrl_c"' in trace_text
    assert '"source": "prompt_request_interrupt"' in trace_text


def test_running_escape_requests_cancel_without_exiting_app():
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

        prompt._handle_escape(event)

        assert cancels == [True]
        assert prompt.cancel_requested is True
        assert event.app.invalidated is True
        assert event.app.exited is False


def test_idle_escape_exits_like_ctrl_c():
    class FakeApp:
        def __init__(self):
            self.result = None

        def exit(self, result=None):
            self.result = result

        def invalidate(self):
            pass

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        event = FakeEvent()

        prompt._handle_escape(event)

        assert event.app.result == "exit"


def test_running_escape_writes_interrupt_trace(monkeypatch, tmp_path):
    trace_path = tmp_path / "interrupt_trace.log"
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE", "1")
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE_PATH", str(trace_path))

    class FakeApp:
        is_running = True

        def exit(self, result=None):
            pass

        def invalidate(self):
            pass

    class FakeEvent:
        def __init__(self):
            self.app = FakeApp()

    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_cancel=lambda: None)
        prompt.begin_running()

        prompt._handle_escape(FakeEvent())

    trace_text = trace_path.read_text(encoding="utf-8")
    assert '"source": "prompt_toolkit_escape"' in trace_text
    assert '"source": "prompt_request_interrupt"' in trace_text


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
        assert prompt._approval_state is not None
        assert prompt._approval_state["submitted"] is True
        assert prompt._approval_state["submitted_index"] == 1
        assert prompt._approval_state["submitted_label"] == "Allow for this session"


def test_prompt_approval_repeat_submit_does_not_repeat_callback():
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
        prompt._submit_approval(0)
        prompt._submit_approval(0)

        assert [choice.value for choice in choices] == ["allow_once"]


def test_prompt_clear_approval_moves_submitted_state_to_history():
    choices = []
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=choices.append)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "git status"},
            approval_key="run:git status",
            command="git status",
        )

        prompt.show_approval(decision)
        prompt._submit_approval(0)
        prompt.clear_approval()

        assert prompt._approval_state is None
        assert len(prompt._approval_history) == 1
        text = fragment_list_to_text(prompt._render_approval())
        assert "approval result" in text
        assert "Decision: [1] Allow once" in text
        assert "Command: git status" in text


def test_prompt_render_approval_shows_history_and_active_approval():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=lambda choice: None)
        first = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "git status"},
            approval_key="run:git status",
            command="git status",
        )
        second = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need second approval",
            tool_name="run_command",
            tool_args={"command": "git diff"},
            approval_key="run:git diff",
            command="git diff",
        )

        prompt.show_approval(first)
        prompt._submit_approval(0)
        prompt.clear_approval()
        prompt.show_approval(second)
        text = fragment_list_to_text(prompt._render_approval())

        assert "approval result" in text
        assert "Decision: [1] Allow once" in text
        assert "Need second approval" in text
        assert "Command: git diff" in text
        assert text.count("[1] Allow once") == 2


def test_prompt_clear_approval_clear_history_removes_history_and_active_state():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=lambda choice: None)
        first = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="run_command",
            tool_args={"command": "git status"},
            approval_key="run:git status",
            command="git status",
        )
        second = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need second approval",
            tool_name="run_command",
            tool_args={"command": "git diff"},
            approval_key="run:git diff",
            command="git diff",
        )

        prompt.show_approval(first)
        prompt._submit_approval(0)
        prompt.clear_approval()
        prompt.show_approval(second)
        prompt.clear_approval(clear_history=True)

        assert prompt._approval_state is None
        assert prompt._approval_history == []
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
