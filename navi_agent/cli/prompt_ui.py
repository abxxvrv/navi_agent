"""Minimal prompt_toolkit input box — Hermes-style.

All conversation content is printed directly to stdout via Rich console.
prompt_toolkit only manages the input area at the bottom of the terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import textwrap
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, VSplit, Window, Dimension
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle

from ..tools.approval import UserApprovalChoice
from .interrupt_trace import trace_interrupt
from .paste_trace import summarize_text, trace_paste


_NAVI_STYLE = PTStyle.from_dict({
    "bottom-toolbar": "bg:#1a1a2e #C0C0C0",
    "running-prompt-separator": "#888888",
    "input": "",
    "picker-selected": "bold",
    "picker-dim": "#888888",
    "approval-countdown": "#4ec9b0",
    "approval-countdown-warn": "bold #ff4444",
})

class NaviPromptSession:
    """Thin wrapper around prompt_toolkit — input box only, no conversation display."""

    def __init__(
        self,
        *,
        history_path: Path,
        completer: Completer | None,
        key_bindings: KeyBindings,
        bottom_toolbar: Callable[[], Any],
        on_cancel: Callable[[], None] | None = None,
        on_approval_response: Callable[[UserApprovalChoice], None] | None = None,
        input: Any = None,
        output: Any = None,
    ) -> None:
        self._running = False
        self._bottom_toolbar = bottom_toolbar
        self._on_cancel = on_cancel
        self._on_approval_response = on_approval_response
        self._custom_io = input is not None or output is not None
        self._idle_queue: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_requested = False
        self._force_exit = False  # Double Ctrl+C → force exit
        self._last_interrupt_time: float = 0.0  # 上次中断键时间（用于 double-press 检测）

        # Model picker state (None = closed)
        self.model_picker: dict | None = None
        self._approval_state: dict[str, Any] | None = None
        self._approval_history: list[dict[str, Any]] = []
        self.approval_broker: Any = None  # set by chat_controller after init
        self._countdown_timer: threading.Timer | None = None

        input_bindings = KeyBindings()
        running_bindings = KeyBindings()
        picker_bindings = KeyBindings()
        approval_bindings = KeyBindings()
        global_bindings = KeyBindings()
        not_running = Condition(lambda: not self._running)
        picker_active = Condition(lambda: self.model_picker is not None)
        approval_active = Condition(lambda: self._approval_state is not None)
        approval_ui_visible = Condition(lambda: self._approval_state is not None or bool(self._approval_history))

        # ── Picker key bindings ──

        @picker_bindings.add("up", filter=picker_active, eager=True)
        def _picker_up(event: KeyPressEvent) -> None:
            if self.model_picker:
                self.model_picker["selected"] = max(0, self.model_picker["selected"] - 1)
                event.app.invalidate()

        @picker_bindings.add("down", filter=picker_active, eager=True)
        def _picker_down(event: KeyPressEvent) -> None:
            if self.model_picker:
                items = self.model_picker.get("items") or []
                self.model_picker["selected"] = min(len(items) - 1, self.model_picker["selected"] + 1)
                event.app.invalidate()

        @picker_bindings.add("enter", filter=picker_active, eager=True)
        def _picker_enter(event: KeyPressEvent) -> None:
            if not self.model_picker:
                return
            state = self.model_picker
            items = state.get("items") or []
            idx = state["selected"]
            if idx >= len(items):
                return
            if state["stage"] == "provider":
                # 选中 provider → 切换到 model 列表
                provider = items[idx]
                state["current_provider"] = provider
                state["stage"] = "model"
                state["selected"] = 0
                # 通过回调获取 model 列表
                if state.get("on_provider_selected"):
                    state["items"] = state["on_provider_selected"](provider)
                event.app.invalidate()
            elif state["stage"] == "model":
                # 选中 model → 执行切换
                model = items[idx]
                provider = state.get("current_provider", "")
                if state.get("on_model_selected"):
                    state["on_model_selected"](provider, model)
                self.close_model_picker()
                event.app.invalidate()

        @picker_bindings.add("escape", filter=picker_active, eager=True)
        def _picker_escape(event: KeyPressEvent) -> None:
            self.close_model_picker()
            event.app.invalidate()

        # ── Approval key bindings ──

        @approval_bindings.add("up", filter=approval_active, eager=True)
        def _approval_up(event: KeyPressEvent) -> None:
            state = self._approval_state
            if state:
                state["selected"] = max(0, state["selected"] - 1)
                event.app.invalidate()

        @approval_bindings.add("down", filter=approval_active, eager=True)
        def _approval_down(event: KeyPressEvent) -> None:
            state = self._approval_state
            if state:
                state["selected"] = min(len(state["choices"]) - 1, state["selected"] + 1)
                event.app.invalidate()

        @approval_bindings.add("enter", filter=approval_active, eager=True)
        def _approval_enter(event: KeyPressEvent) -> None:
            self._submit_approval()
            event.app.invalidate()

        @approval_bindings.add("escape", filter=approval_active, eager=True)
        def _approval_escape(event: KeyPressEvent) -> None:
            self._handle_escape(event)

        def _make_approval_number_handler(index: int):
            def _handler(event: KeyPressEvent) -> None:
                self._submit_approval(index=index)
                event.app.invalidate()
            return _handler

        for _num in range(1, 4):
            approval_bindings.add(str(_num), filter=approval_active, eager=True)(
                _make_approval_number_handler(_num - 1)
            )

        @approval_bindings.add("c-o", filter=approval_active, eager=True)
        def _approval_expand(event: KeyPressEvent) -> None:
            state = self._approval_state
            if state:
                state["command_expanded"] = not state.get("command_expanded", False)
                event.app.invalidate()

        @approval_bindings.add(Keys.Any, filter=approval_ui_visible, eager=True)
        def _approval_swallow_input(event: KeyPressEvent) -> None:
            event.app.invalidate()

        # ── BracketedPaste binding (before Enter bindings) ──

        @global_bindings.add(Keys.BracketedPaste, eager=True, is_global=True)
        def _handle_bracketed_paste(event: KeyPressEvent) -> None:
            data = event.data.replace("\r\n", "\n").replace("\r", "\n")
            trace_paste(
                "bracketed_paste_seen",
                text_summary=summarize_text(data),
                running=self._running,
                approval_active=self._approval_state is not None,
                picker_active=self.model_picker is not None,
            )
            # Only insert text in idle state; modal states (approval/picker)
            # have their own input handling that must not be bypassed.
            if self._running or self._approval_state is not None or self.model_picker is not None:
                return
            event.current_buffer.insert_text(data)
            trace_paste(
                "bracketed_paste_inserted",
                text_summary=summarize_text(data),
                buffer_len=len(event.current_buffer.text),
                running=self._running,
                approval_active=self._approval_state is not None,
                picker_active=self.model_picker is not None,
            )

        # ── Running key bindings ──

        @running_bindings.add("enter", eager=True, filter=Condition(lambda: self._running and self._approval_state is None and not self._approval_history))
        def _(event: KeyPressEvent) -> None:
            trace_paste(
                "running_enter_newline",
                running=self._running,
                approval_active=self._approval_state is not None,
                picker_active=self.model_picker is not None,
                buffer_len=len(event.current_buffer.text),
            )
            event.current_buffer.insert_text("\n")
            event.app.invalidate()

        # ── Idle key bindings ──

        @input_bindings.add("/", eager=True, filter=not_running & ~picker_active & ~approval_ui_visible)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("/")
            event.current_buffer.start_completion(select_first=False)
            event.app.invalidate()

        @input_bindings.add("enter", eager=True, filter=not_running & ~picker_active & ~approval_ui_visible)
        def _(event: KeyPressEvent) -> None:
            text = event.current_buffer.text.strip()
            if text:
                trace_paste(
                    "idle_enter_seen",
                    text_summary=summarize_text(text),
                    running=self._running,
                    approval_active=self._approval_state is not None,
                    picker_active=self.model_picker is not None,
                    buffer_len=len(event.current_buffer.text),
                )
                event.current_buffer.reset()
                trace_paste(
                    "idle_enter_submit",
                    text_summary=summarize_text(text),
                    queue_size=self._idle_queue.qsize(),
                    running=self._running,
                    approval_active=self._approval_state is not None,
                    picker_active=self.model_picker is not None,
                )
                self._idle_queue.put_nowait(text)
                trace_paste(
                    "idle_queue_put",
                    text_summary=summarize_text(text),
                    queue_size=self._idle_queue.qsize(),
                    running=self._running,
                    approval_active=self._approval_state is not None,
                    picker_active=self.model_picker is not None,
                )

        @input_bindings.add("escape", "enter", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @input_bindings.add("c-j", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @input_bindings.add("c-d", eager=True, filter=not_running & ~picker_active & ~approval_ui_visible)
        def _(event: KeyPressEvent) -> None:
            event.app.exit(result="exit")

        @global_bindings.add("c-c", eager=True, is_global=True)
        def _(event: KeyPressEvent) -> None:
            self._handle_ctrl_c(event)

        @global_bindings.add("escape", eager=True, is_global=True)
        def _(event: KeyPressEvent) -> None:
            self._handle_escape(event)

        self._buffer = Buffer(
            history=FileHistory(str(history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=False,
        )

        self._layout = Layout(
            FloatContainer(
                content=HSplit([
                Window(
                    content=FormattedTextControl(self._render_model_picker),
                    dont_extend_height=True,
                    height=self._picker_height,
                ),
                Window(
                    content=FormattedTextControl(self._render_approval),
                    dont_extend_height=True,
                    height=self._approval_height,
                ),
                Window(
                    content=FormattedTextControl(self._render_box_top),
                    height=1,
                    dont_extend_height=True,
                ),
                VSplit([
                    Window(
                        content=FormattedTextControl(lambda: "│ > "),
                        width=4,
                        dont_extend_height=True,
                    ),
                    Window(
                        content=BufferControl(buffer=self._buffer),
                        height=Dimension(min=1, max=6),
                        dont_extend_height=True,
                        wrap_lines=True,
                    ),
                    Window(
                        content=FormattedTextControl(lambda: "│"),
                        width=1,
                        dont_extend_height=True,
                    ),
                ]),
                Window(
                    content=FormattedTextControl(self._render_box_bottom),
                    height=1,
                    dont_extend_height=True,
                ),
                Window(
                    content=FormattedTextControl(self._render_toolbar),
                    height=2,
                    dont_extend_height=True,
                ),
                ]),
                floats=[
                    Float(
                        xcursor=True,
                        ycursor=True,
                        content=CompletionsMenu(max_height=8, scroll_offset=1),
                    ),
                ],
            )
        )

        self._app: Application[str] = Application(
            layout=self._layout,
            key_bindings=merge_key_bindings([global_bindings, picker_bindings, approval_bindings, running_bindings, input_bindings, key_bindings]),
            input=input,
            output=output,
            full_screen=False,
            style=_NAVI_STYLE,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_session(
        self,
        *,
        on_submit: Callable[[str], Awaitable[None]],
    ) -> None:
        self._loop = asyncio.get_running_loop()

        async def message_loop() -> None:
            while True:
                text = await self._idle_queue.get()
                trace_paste(
                    "idle_queue_get",
                    text_summary=summarize_text(text),
                    queue_size=self._idle_queue.qsize(),
                )
                await on_submit(text)

        task = asyncio.ensure_future(message_loop())
        try:
            with self._patch_stdout():
                await self._app.run_async()
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._loop = None

    def begin_running(self) -> None:
        trace_interrupt("prompt_begin_running")
        trace_paste(
            "prompt_begin_running_paste",
            running=self._running,
            cancel_requested=self._cancel_requested,
            force_exit=self._force_exit,
        )
        self._running = True
        self._cancel_requested = False
        self._force_exit = False
        self._app.invalidate()

    def end_running(self) -> None:
        trace_interrupt(
            "prompt_end_running",
            cancel_requested=self._cancel_requested,
            force_exit=self._force_exit,
        )
        trace_paste(
            "prompt_end_running_paste",
            running=self._running,
            cancel_requested=self._cancel_requested,
            force_exit=self._force_exit,
        )
        self._running = False
        self._app.invalidate()

    def show_approval(self, decision: Any) -> None:
        """Show an approval prompt. User choice is sent through callback."""
        self._approval_state = {
            "decision": decision,
            "choices": [
                ("Allow once", UserApprovalChoice.ALLOW_ONCE),
                ("Allow for this session", UserApprovalChoice.ALLOW_SESSION),
                ("Reject", UserApprovalChoice.REJECT),
            ],
            "selected": 0,
            "command_expanded": False,
            "submitted": False,
        }
        # Cancel any existing countdown, then start a new one
        if self._countdown_timer is not None:
            self._countdown_timer.cancel()

        def _tick() -> None:
            if self._approval_state is not None:
                self.invalidate()
                self._countdown_timer = threading.Timer(1.0, _tick)
                self._countdown_timer.daemon = True
                self._countdown_timer.start()

        self._countdown_timer = threading.Timer(1.0, _tick)
        self._countdown_timer.daemon = True
        self._countdown_timer.start()
        self.invalidate()

    def clear_approval(self, *, clear_history: bool = False) -> None:
        if self._approval_state is not None and self._approval_state.get("submitted"):
            self._approval_history.append(self._approval_state)
        if self._countdown_timer is not None:
            self._countdown_timer.cancel()
            self._countdown_timer = None
        self._approval_state = None
        if clear_history:
            self._approval_history.clear()
        self.invalidate()

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    @property
    def force_exit(self) -> bool:
        return self._force_exit

    def invalidate(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._app.invalidate)
        else:
            self._app.invalidate()

    def exit(self, result: str = "exit") -> None:
        self._app.exit(result=result)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def can_handle_interrupt_signal(self) -> bool:
        return self._running or self._approval_state is not None or self.model_picker is not None

    def handle_interrupt_signal(self) -> bool:
        trace_interrupt(
            "prompt_signal_interrupt",
            running=self._running,
            approval_active=self._approval_state is not None,
            model_picker_active=self.model_picker is not None,
            cancel_requested=self._cancel_requested,
            force_exit=self._force_exit,
        )
        if self._approval_state is not None:
            self._cancel_approval()
            self.invalidate()
            return True

        if self.model_picker is not None:
            self.close_model_picker()
            self.invalidate()
            return True

        if self._running:
            self._request_interrupt()
            self.invalidate()
            return True

        return False

    # ── Model picker ──

    def open_model_picker(
        self,
        *,
        providers: list[str],
        current_provider: str,
        current_model: str,
        on_provider_selected: Callable[[str], list[str]],
        on_model_selected: Callable[[str, str], None],
    ) -> None:
        """Open the model picker. First stage: select provider."""
        current_idx = providers.index(current_provider) if current_provider in providers else 0
        self.model_picker = {
            "stage": "provider",
            "items": providers,
            "selected": current_idx,
            "current_provider": current_provider,
            "current_model": current_model,
            "on_provider_selected": on_provider_selected,
            "on_model_selected": on_model_selected,
        }
        self._app.invalidate()

    def close_model_picker(self) -> None:
        self.model_picker = None
        self._app.invalidate()

    def _submit_approval(self, index: int | None = None) -> None:
        state = self._approval_state
        if not state:
            return
        if index is None:
            index = state.get("selected", 0)
        choices = state.get("choices") or []
        if index < 0 or index >= len(choices):
            index = len(choices) - 1
        if state.get("submitted"):
            return
        label, value = choices[index]
        state["selected"] = index
        state["submitted"] = True
        state["submitted_index"] = index
        state["submitted_label"] = label
        state["submitted_choice"] = value
        if self._on_approval_response:
            self._on_approval_response(value)
        self._app.invalidate()

    def _cancel_approval(self) -> None:
        if not self._approval_state:
            return
        trace_interrupt("prompt_cancel_approval")
        self._cancel_requested = True
        self._approval_state = None
        if self._on_cancel:
            self._on_cancel()
        self._app.invalidate()

    def _handle_ctrl_c(self, event: KeyPressEvent) -> None:
        self._handle_interrupt_key(event, "prompt_toolkit_ctrl_c")

    def _handle_escape(self, event: KeyPressEvent) -> None:
        self._handle_interrupt_key(event, "prompt_toolkit_escape")

    def _handle_interrupt_key(self, event: KeyPressEvent, source: str) -> None:
        trace_interrupt(
            source,
            running=self._running,
            approval_active=self._approval_state is not None,
            model_picker_active=self.model_picker is not None,
            cancel_requested=self._cancel_requested,
            force_exit=self._force_exit,
            app_running=getattr(event.app, "is_running", None),
        )
        if self._approval_state is not None:
            self._cancel_approval()
            event.app.invalidate()
            return

        if self.model_picker is not None:
            self.close_model_picker()
            event.app.invalidate()
            return

        if self._running:
            self._request_interrupt()
            event.app.invalidate()
            return

        event.app.exit(result="exit")

    def _request_interrupt(self) -> None:
        import time as _time

        now = _time.monotonic()
        self._cancel_requested = True
        if now - self._last_interrupt_time < 2.0:
            self._force_exit = True
        else:
            self._last_interrupt_time = now

        trace_interrupt(
            "prompt_request_interrupt",
            force_exit=self._force_exit,
            on_cancel=self._on_cancel is not None,
        )

        if self._on_cancel:
            self._on_cancel()

    def _picker_height(self) -> int:
        """Dynamic height for the picker window."""
        if not self.model_picker:
            return 0
        items = self.model_picker.get("items") or []
        # items + stage label + hint
        return len(items) + 2

    def _render_model_picker(self) -> FormattedText:
        """Render the model picker if active."""
        if not self.model_picker:
            return FormattedText()

        state = self.model_picker
        items = state.get("items") or []
        selected = state.get("selected", 0)
        stage = state.get("stage", "provider")
        current_model = state.get("current_model", "")

        fragments = FormattedText()
        label = "Providers" if stage == "provider" else "Models"
        fragments.append(("", f"  {label}:\n"))

        for i, item in enumerate(items):
            if i == selected:
                prefix = "❯ "
                style = "class:picker-selected"
            else:
                prefix = "  "
                style = "class:picker-dim"

            # 标记当前项
            suffix = ""
            if stage == "provider" and item == state.get("current_provider"):
                suffix = " (current)"
            elif stage == "model" and item == current_model:
                suffix = " ◄"

            fragments.append((style, f"  {prefix}{item}{suffix}\n"))

        fragments.append(("class:picker-dim", "  ↑↓ navigate  Enter select  Esc cancel\n"))
        return fragments

    def _approval_height(self) -> int:
        if not self._approval_state and not self._approval_history:
            return 0
        return len(self._approval_lines())

    def _render_approval(self) -> FormattedText:
        if not self._approval_state and not self._approval_history:
            return FormattedText()
        fragments = FormattedText()
        for style, text in self._approval_lines():
            fragments.append((style, text + "\n"))
        return fragments

    def _approval_lines(self) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        for state in self._approval_history:
            decision = state["decision"]
            command = getattr(decision, "command", None) or (getattr(decision, "tool_args", {}) or {}).get("command", "")
            index = state.get("submitted_index", state.get("selected", 0))
            label = state.get("submitted_label", "")
            lines.extend([
                ("class:running-prompt-separator", "╭─ approval result " + "─" * 60),
                ("", f"  Tool: {getattr(decision, 'tool_name', '')}"),
                ("", f"  Risk: {getattr(getattr(decision, 'risk', ''), 'value', getattr(decision, 'risk', ''))}"),
                ("", f"  Decision: [{index + 1}] {label}"),
            ])
            if command:
                lines.append(("", f"  Command: {command}"))
            lines.append(("class:running-prompt-separator", "╰" + "─" * 79))
        state = self._approval_state
        if not state:
            return lines
        decision = state["decision"]
        command = getattr(decision, "command", None) or (getattr(decision, "tool_args", {}) or {}).get("command", "")
        lines.extend([
            ("class:running-prompt-separator", "╭─ approval " + "─" * 68),
            ("", f"  {getattr(decision, 'reason', '')}"),
            ("", ""),
            ("", f"  Tool: {getattr(decision, 'tool_name', '')}"),
            ("", f"  Risk: {getattr(getattr(decision, 'risk', ''), 'value', getattr(decision, 'risk', ''))}"),
        ])
        if command:
            expanded = state.get("command_expanded", False)
            max_chars = 3000 if expanded else 300
            max_lines = 80 if expanded else 10
            display_command = command
            visible_chars = min(len(command), max_chars)
            if len(display_command) > max_chars:
                display_command = display_command[:max_chars] + "..."
            app = get_app_or_none()
            columns = app.output.get_size().columns if app is not None else 80
            wrap_width = max(40, min(120, columns - 12))
            cmd_lines: list[str] = []
            for source_line in display_command.split("\n"):
                wrapped = textwrap.wrap(
                    source_line,
                    width=wrap_width,
                    break_long_words=True,
                    break_on_hyphens=False,
                    replace_whitespace=False,
                    drop_whitespace=False,
                )
                cmd_lines.extend(wrapped or [""])
            total_lines = len(cmd_lines)
            visible_lines = min(len(cmd_lines), max_lines)
            if len(cmd_lines) > max_lines:
                cmd_lines = cmd_lines[:max_lines]
                cmd_lines.append("...")
            lines.append(("", f"  Command: {cmd_lines[0]}"))
            for extra in cmd_lines[1:]:
                lines.append(("", f"         {extra}"))
            mode = "expanded" if expanded else "collapsed"
            next_action = "collapse" if expanded else "expand"
            lines.append((
                "class:bottom-toolbar",
                f"  Command preview: {mode}, showing {visible_chars}/{len(command)} chars, "
                f"{visible_lines}/{total_lines} lines. Ctrl+O {next_action}.",
            ))
        lines.append(("class:running-prompt-separator", "╰" + "─" * 79))
        lines.append(("class:bottom-toolbar", "Use ↑/↓ or 1/2/3, then Enter. Ctrl+O toggle command."))

        # Countdown line
        broker = self.approval_broker
        if broker is not None and broker._current_deadline is not None:
            remaining = max(0.0, broker._current_deadline - time.monotonic())
            if remaining > 0:
                secs = int(remaining)
                style = "class:approval-countdown-warn" if secs <= 10 else "class:approval-countdown"
                lines.append((style, f"⏱ Auto-reject in {secs}s"))

        lines.append(("", ""))
        selected = state.get("selected", 0)
        for i, (label, _value) in enumerate(state["choices"], start=1):
            prefix = "❯" if selected == i - 1 else " "
            style = "class:picker-selected" if selected == i - 1 else ""
            lines.append((style, f"{prefix} [{i}] {label}"))
        return lines

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _patch_stdout(self):
        if self._custom_io:
            return contextlib.nullcontext()
        return patch_stdout(raw=True)

    def _render_box_top(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80
        return FormattedText([("class:running-prompt-separator", _box_top(columns))])

    def _render_box_bottom(self) -> FormattedText:
        app = get_app_or_none()
        columns = app.output.get_size().columns if app is not None else 80
        return FormattedText([("class:running-prompt-separator", _box_bottom(columns))])

    def _render_toolbar(self) -> FormattedText:
        fragments = FormattedText()
        fragments.extend(self._bottom_toolbar())
        if self._running:
            fragments.append(("", "\n"))
            if self._approval_state:
                fragments.append(("class:bottom-toolbar", "approval: use ↑/↓ or 1/2/3, then Enter"))
            elif self._cancel_requested:
                fragments.append(("class:bottom-toolbar", "interrupt requested  |  waiting for current operation"))
            else:
                fragments.append(("class:bottom-toolbar", "enter: newline  |  ctrl+c/esc: interrupt"))
        return fragments


def _box_top(columns: int) -> str:
    return "╭" + ("─" * max(columns - 2, 0)) + "╮"


def _box_bottom(columns: int) -> str:
    return "╰" + ("─" * max(columns - 2, 0)) + "╯"
