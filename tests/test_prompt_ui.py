import asyncio
import tempfile
import time
from io import StringIO
from pathlib import Path

from prompt_toolkit.document import Document
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from navi_agent.approval import ApprovalAction, ApprovalDecision, RiskLevel
import navi_agent.cli as cli
from navi_agent.prompt_ui import NaviPromptSession
from navi_agent.ui import NaviInlineStreamState


def _make_prompt(pipe_input):
    return NaviPromptSession(
        history_path=Path(tempfile.gettempdir()) / "navi_prompt_test_history.txt",
        completer=None,
        key_bindings=KeyBindings(),
        bottom_toolbar=lambda: [("", "toolbar")],
        input=pipe_input,
        output=DummyOutput(),
    )


def test_prompt_renders_streaming_above_input_box():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        state = NaviInlineStreamState()
        state.handle_event({"type": "assistant_delta", "content": "OK"})
        prompt.begin_streaming(state)

        text = prompt._conv_buffer.text

    # During active streaming, _compose_current shows a "Composing..." indicator
    assert "Composing" in text


def test_prompt_shows_history_after_streaming():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        state = NaviInlineStreamState()
        state.handle_event({"type": "assistant_delta", "content": "OK"})
        state.handle_event({"type": "assistant_end"})
        prompt.show_history(state.render())

        text = prompt._conv_buffer.text

        assert "OK" in text


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


def test_inline_state_keeps_submitted_user_message():
    state = NaviInlineStreamState()
    state.add_user_message("你好")
    state.handle_event({"type": "assistant_delta", "content": "OK"})
    state.handle_event({"type": "assistant_end"})

    output = StringIO()
    test_console = Console(file=output, force_terminal=False, width=80, highlight=False)
    test_console.print(state.render())
    text = output.getvalue()

    assert "你好" in text
    assert "OK" in text
    assert text.index("你好") < text.index("OK")


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


def test_prompt_approval_returns_choice():
    """request_approval blocks until _choose_approval resolves it."""
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

        # Resolve approval from another thread
        def resolve():
            time.sleep(0.05)
            prompt._choose_approval(1)  # "Allow for this session"

        import threading
        t = threading.Thread(target=resolve)
        t.start()

        choice = prompt.request_approval(decision)
        t.join()

        assert choice.value == "allow_session"


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


def test_stream_state_accumulates_across_turns():
    """A single NaviInlineStreamState reused across turns keeps all history."""
    state = NaviInlineStreamState()

    def render(state):
        output = StringIO()
        c = Console(file=output, force_terminal=False, width=80, highlight=False)
        rendered = state.render()
        if rendered is not None:
            c.print(rendered)
        return output.getvalue()

    # Turn 1
    state.add_user_message("message one")
    state.handle_event({"type": "assistant_delta", "content": "response one"})
    state.handle_event({"type": "assistant_end"})

    rendered_1 = render(state)
    assert "message one" in rendered_1
    assert "response one" in rendered_1

    # Turn 2 — same state, should still contain turn 1 history
    state.add_user_message("message two")
    state.handle_event({"type": "assistant_delta", "content": "response two"})
    state.handle_event({"type": "assistant_end"})

    rendered_2 = render(state)
    assert "message one" in rendered_2
    assert "response one" in rendered_2
    assert "message two" in rendered_2
    assert "response two" in rendered_2


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
