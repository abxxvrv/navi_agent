import asyncio
import tempfile
import time
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


async def _run_prompt_input(
    prompt,
    pipe_input,
    writer,
    *,
    exit_after_submits=1,
    timeout=2.0,
):
    submitted = []

    async def on_submit(text):
        submitted.append(text)
        if exit_after_submits is not None and len(submitted) >= exit_after_submits:
            prompt.exit()

    task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
    await asyncio.sleep(0.05)
    await writer()
    await asyncio.wait_for(task, timeout=timeout)
    return submitted


async def _send_slow_text(pipe_input, text, delay=0.09):
    for char in text:
        pipe_input.send_text(char)
        await asyncio.sleep(delay)


def test_idle_enter_submits_plain_input_after_guard():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)

            async def writer():
                await _send_slow_text(pipe_input, "hello")
                pipe_input.send_text("\r")

            submitted = await _run_prompt_input(prompt, pipe_input, writer)

            assert submitted == ["hello"]
            assert prompt._pending_idle_enter_task is None

    asyncio.run(run())


def test_idle_enter_submits_fast_plain_input_after_guard():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)

            async def writer():
                pipe_input.send_text("hellox\r")

            submitted = await _run_prompt_input(prompt, pipe_input, writer)

            assert submitted == ["hellox"]
            assert prompt._pending_idle_enter_task is None

    asyncio.run(run())


def test_idle_fast_multiline_key_stream_stays_in_buffer_after_stable():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("line1\rline2\rline3")
            await asyncio.sleep(0.40)

            assert submitted == []
            assert prompt._buffer.text == "line1\nline2\nline3"
            assert prompt._paste_capture_active is False

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_idle_short_multiline_key_stream_stays_in_buffer_after_stable():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("a\rb\rc")
            await asyncio.sleep(0.40)

            assert submitted == []
            assert prompt._buffer.text == "a\nb\nc"
            assert prompt._paste_capture_active is False

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_idle_fast_multiline_key_stream_submits_once_after_user_enter():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)
                prompt.exit()

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("line1\rline2\rline3")
            await asyncio.sleep(0.40)
            assert submitted == []

            pipe_input.send_text("\r")
            await asyncio.wait_for(task, timeout=1.0)
            assert submitted == ["line1\nline2\nline3"]

    asyncio.run(run())


def test_bracketed_paste_multiline_stays_in_buffer_without_submit():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("\x1b[200~line1\nline2\x1b[201~")
            await asyncio.sleep(0.20)

            assert submitted == []
            assert prompt._buffer.text == "line1\nline2"

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_bracketed_paste_enter_submits_after_guard():
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)

            async def writer():
                pipe_input.send_text("\x1b[200~line1\nline2\x1b[201~\r")

            submitted = await _run_prompt_input(prompt, pipe_input, writer)

            assert submitted == ["line1\nline2"]
            assert prompt._pending_idle_enter_task is None

    asyncio.run(run())


def test_prompt_approval_panel_renders_decision():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="bash",
            tool_args={"command": "echo hi"},
            approval_key="run:echo hi",
            command="echo hi",
        )

        prompt.show_approval(decision)
        text = fragment_list_to_text(prompt._render_approval())

    assert "Need approval" in text
    assert "bash" in text
    assert "echo hi" in text


def test_prompt_approval_command_preview_limits_chars_and_hides_key():
    long_command = "x" * 4000
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        decision = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="bash",
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
            tool_name="bash",
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


def test_running_toolbar_shows_tool_status():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input)
        prompt.begin_running()

        prompt.set_tool_status("grep", "query=needle, path=.")
        text = fragment_list_to_text(prompt._render_toolbar())

        assert "using grep" in text
        assert "query=needle" in text
        assert "enter: newline" not in text

        prompt.clear_tool_status("read_file")
        assert "using grep" in fragment_list_to_text(prompt._render_toolbar())

        prompt.clear_tool_status("grep")
        assert "enter: newline" in fragment_list_to_text(prompt._render_toolbar())


def test_ctrl_g_only_backgrounds_a_running_non_approval_prompt():
    async def run():
        backgrounds = []
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(
                pipe_input,
                on_background=lambda: backgrounds.append(True),
            )

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("\x07")
            await asyncio.sleep(0.05)
            assert backgrounds == []

            prompt.begin_running()
            pipe_input.send_text("\x07")
            await asyncio.sleep(0.05)
            assert backgrounds == [True]

            prompt.show_approval(
                ApprovalDecision(
                    action=ApprovalAction.ASK,
                    risk=RiskLevel.RISKY,
                    reason="Need approval",
                    tool_name="bash",
                    tool_args={"command": "echo hi"},
                    approval_key="run:echo hi",
                    command="echo hi",
                )
            )
            pipe_input.send_text("\x07")
            await asyncio.sleep(0.05)
            assert backgrounds == [True]

            prompt.clear_approval()
            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_single_prompt_queue_passes_synthetic_and_user_origins():
    async def run():
        submitted = []
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)

            async def on_submit(text, images, origin):
                submitted.append((text, images, origin))
                if len(submitted) == 2:
                    prompt.exit()

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            prompt._idle_queue.put_nowait(("background done", [], "task:t1"))
            pipe_input.send_text("hello")
            await asyncio.sleep(0.10)
            pipe_input.send_text("\r")

            await asyncio.wait_for(task, timeout=1.0)
            assert submitted == [
                ("background done", [], "task:t1"),
                ("hello", [], "user"),
            ]

    asyncio.run(run())


def test_tool_result_prints_bounded_argument_summary():
    lines = []
    cli.print_agent_event(
        {
            "type": "tool_result",
            "tool_name": "search_session",
            "tool_args": {"query": "needle", "extra": "x" * 200},
            "tool_result": {"ok": True},
            "elapsed": 0.2,
        },
        printer=lines.append,
    )

    assert len(lines) == 1
    assert "search_session" in lines[0]
    assert "query=needle" in lines[0]
    assert "..." in lines[0]
    assert len(lines[0]) < 180


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
            tool_name="bash",
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
            tool_name="bash",
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
            tool_name="bash",
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
            tool_name="bash",
            tool_args={"command": "git status"},
            approval_key="run:git status",
            command="git status",
        )
        second = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need second approval",
            tool_name="bash",
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


def test_prompt_approval_history_multiline_command_height_matches_rendered_lines():
    command = 'python -c "\nprint(1)\nprint(2)\nprint(3)\nprint(4)\nprint(5)\nprint(6)\n"'
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=lambda choice: None)
        first = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="bash",
            tool_args={"command": command},
            approval_key="run:multiline",
            command=command,
        )
        second = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need second approval",
            tool_name="bash",
            tool_args={"command": "git diff"},
            approval_key="run:git diff",
            command="git diff",
        )

        prompt.show_approval(first)
        prompt._submit_approval(0)
        prompt.clear_approval()
        prompt.show_approval(second)
        lines = prompt._approval_lines()
        text = fragment_list_to_text(prompt._render_approval())

        assert all("\n" not in line for _style, line in lines)
        assert prompt._approval_height() == text.count("\n")
        assert "approval result" in text
        assert "Need second approval" in text
        assert "Command: git diff" in text


def test_prompt_clear_approval_clear_history_removes_history_and_active_state():
    with create_pipe_input() as pipe_input:
        prompt = _make_prompt(pipe_input, on_approval_response=lambda choice: None)
        first = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need approval",
            tool_name="bash",
            tool_args={"command": "git status"},
            approval_key="run:git status",
            command="git status",
        )
        second = ApprovalDecision(
            action=ApprovalAction.ASK,
            risk=RiskLevel.RISKY,
            reason="Need second approval",
            tool_name="bash",
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
            tool_name="bash",
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


def test_gap_large_immediate_submit_no_guard():
    """Enter pressed after a long pause -> immediate submit; no guard task created."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)

            async def writer():
                pipe_input.send_text("hi")
                # Simulate a large gap: set _last_char_at to well in the past
                prompt._last_char_at = prompt._last_char_at - 0.5  # 500ms ago
                pipe_input.send_text("\r")

            submitted = await _run_prompt_input(prompt, pipe_input, writer)

            assert submitted == ["hi"]
            assert prompt._pending_idle_enter_task is None

    asyncio.run(run())


def test_enter_in_paste_capture_never_submits_even_with_large_gap():
    """While a paste streams in, Enter is always a newline — never a submit,
    even if the gap looks large. Batched key-stream pastes have large inter-batch
    gaps, so a mid-stream Enter must not be misread as a manual submit (which
    would split the paste)."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Fast-stream two lines to trigger paste capture
            pipe_input.send_text("line1\rline2")
            await asyncio.sleep(0.05)  # short enough to stay in capture window
            assert prompt._paste_capture_active is True

            # Even with a large gap, Enter must NOT submit while in capture.
            prompt._last_char_at = time.monotonic() - 0.5  # 500ms ago
            pipe_input.send_text("\r")
            await asyncio.sleep(0.05)

            assert submitted == []
            assert prompt._paste_capture_active is True
            assert "\n" in prompt._buffer.text

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_gap_small_inside_paste_capture_inserts_newline_stays_in_capture():
    """Enter with small gap while paste_capture_active -> newline inserted, still in capture, nothing queued."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Fast-stream to trigger paste capture
            pipe_input.send_text("line1\rline2")
            await asyncio.sleep(0.05)

            assert prompt._paste_capture_active is True
            # Force a deterministically tiny gap (don't rely on wall-clock, which
            # can drift past _MANUAL_SUBMIT_GAP_SECONDS under load and flip the branch).
            prompt._last_char_at = time.monotonic()
            pipe_input.send_text("\r")
            await asyncio.sleep(0.02)

            # Nothing should be submitted yet; buffer should have the extra newline
            assert submitted == []
            assert prompt._paste_capture_active is True
            assert "\n" in prompt._buffer.text

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


# ─── Paste-collapse integration tests ────────────────────────────────────────

_BIG_PASTE = "\n".join(f"line {i}" for i in range(10))  # 10 lines, well above threshold
_SMALL_PASTE = "line1\nline2"  # 2 lines, well below threshold


def _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch):
    """Create a prompt with NAVI_HOME isolated to tmp_path."""
    import os
    monkeypatch.setenv("NAVI_HOME", str(tmp_path))
    return _make_prompt(pipe_input)


def test_bracketed_paste_small_not_collapsed(tmp_path, monkeypatch):
    """Small paste (below thresholds) stays in buffer verbatim."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text(f"\x1b[200~{_SMALL_PASTE}\x1b[201~")
            await asyncio.sleep(0.20)

            assert prompt._buffer.text == _SMALL_PASTE
            assert prompt._paste_counter == 0

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_bracketed_paste_large_collapsed(tmp_path, monkeypatch):
    """Large paste (>=8 lines) -> buffer becomes placeholder, file exists, counter incremented."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text(f"\x1b[200~{_BIG_PASTE}\x1b[201~")
            await asyncio.sleep(0.20)

            buf = prompt._buffer.text
            assert buf.startswith("[Pasted text #1:")
            assert "lines ->" in buf
            assert prompt._paste_counter == 1
            # The paste file should exist
            pastes = list((tmp_path / "pastes").glob("paste_*.txt"))
            assert len(pastes) == 1
            assert pastes[0].read_text(encoding="utf-8") == _BIG_PASTE

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_bracketed_paste_prefix_preserved(tmp_path, monkeypatch):
    """Type a prefix, then paste large text -> buffer == prefix + placeholder."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Type prefix slowly so it's below collapse threshold
            await _send_slow_text(pipe_input, "look: ")
            # Now paste large text
            pipe_input.send_text(f"\x1b[200~{_BIG_PASTE}\x1b[201~")
            await asyncio.sleep(0.20)

            buf = prompt._buffer.text
            assert buf.startswith("look: [Pasted text #1:")
            assert prompt._paste_counter == 1

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_fallback_no_false_collapse_ime_chars(tmp_path, monkeypatch):
    """Short multi-char insert below threshold (IME style) is NOT collapsed."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Simulate IME-style commit: 12 Chinese chars, all at once, no newlines
            # This is well below both _COLLAPSE_MIN_CHARS (2000) and _COLLAPSE_MIN_LINES (8)
            ime_text = "你好世界这是测试文本哈哈"  # 12 chars, 0 newlines
            pipe_input.send_text(ime_text)
            await asyncio.sleep(0.10)

            assert prompt._buffer.text == ime_text
            assert prompt._paste_counter == 0

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_fallback_collapses_large_char_insert(tmp_path, monkeypatch):
    """Single buffer change inserting >=2000 chars via fallback path collapses."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Directly manipulate buffer to simulate a large non-bracketed paste
            big_text = "x" * 2500
            prompt._buffer.text = big_text
            await asyncio.sleep(0.10)

            buf = prompt._buffer.text
            assert "[Pasted text #1:" in buf
            assert prompt._paste_counter == 1

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_fallback_slash_not_collapsed(tmp_path, monkeypatch):
    """Buffer text starting with / receiving large insert is NOT collapsed (slash guard)."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)

            async def on_submit(text):
                pass

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            # Directly set buffer to a slash command that happens to be very long
            slash_text = "/" + "x" * 2500
            prompt._buffer.text = slash_text
            await asyncio.sleep(0.10)

            # Should NOT have been collapsed
            assert prompt._paste_counter == 0
            assert not prompt._buffer.text.startswith("[Pasted text")

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_key_stream_large_collapses_at_stable_end(tmp_path, monkeypatch):
    """Large key-stream paste -> buffer collapses to placeholder when capture window stabilises."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            big_key_stream = "\r".join(f"line{i}" for i in range(12))
            pipe_input.send_text(big_key_stream)
            # Wait longer than the 0.25s stability window
            await asyncio.sleep(0.40)

            assert submitted == []
            buf = prompt._buffer.text
            assert buf.startswith("[Pasted text"), f"expected placeholder, got: {buf!r}"
            pastes = list((tmp_path / "pastes").glob("paste_*.txt"))
            assert len(pastes) == 1
            assert prompt._paste_counter == 1

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_key_stream_large_enter_midstream_does_not_submit_collapses_at_stable_end(tmp_path, monkeypatch):
    """A large key-stream paste must not be submitted by an Enter landing mid-stream
    (batched delivery makes the gap look large). It collapses into a SINGLE
    placeholder only when the capture window stabilizes, and stays in the buffer."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            big_key_stream = "\r".join(f"line{i}" for i in range(12))
            pipe_input.send_text(big_key_stream)
            await asyncio.sleep(0.05)  # stay in capture window
            assert prompt._paste_capture_active is True

            # An Enter mid-stream with a large gap must NOT submit.
            prompt._last_char_at = time.monotonic() - 0.5
            pipe_input.send_text("\r")
            await asyncio.sleep(0.05)
            assert submitted == []

            # After the window stabilizes, it collapses once into a placeholder.
            await asyncio.sleep(0.35)
            assert submitted == []
            assert prompt._buffer.text.startswith("[Pasted text")
            pastes = list((tmp_path / "pastes").glob("paste_*.txt"))
            assert len(pastes) == 1

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_key_stream_small_not_collapsed_after_stable(tmp_path, monkeypatch):
    """Small key-stream paste (below threshold) stays in buffer unchanged after capture stabilises."""
    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt_with_pastes(pipe_input, tmp_path, monkeypatch)
            submitted = []

            async def on_submit(text):
                submitted.append(text)

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)

            pipe_input.send_text("a\rb\rc")
            await asyncio.sleep(0.40)

            assert submitted == []
            assert prompt._buffer.text == "a\nb\nc"
            assert prompt._paste_counter == 0

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


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
