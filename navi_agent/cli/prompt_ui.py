"""Minimal prompt_toolkit input box — Hermes-style.

All conversation content is printed directly to stdout via Rich console.
prompt_toolkit only manages the input area at the bottom of the terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
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
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import Float, FloatContainer, HSplit, VSplit, Window, Dimension
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle

from ..tools.approval import UserApprovalChoice


_NAVI_STYLE = PTStyle.from_dict({
    "bottom-toolbar": "bg:#1a1a2e #C0C0C0",
    "running-prompt-separator": "#888888",
    "input": "",
    "picker-selected": "bold",
    "picker-dim": "#888888",
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
        self._last_ctrl_c_time: float = 0.0  # 上次 Ctrl+C 时间（用于 double-press 检测）

        # Model picker state (None = closed)
        self.model_picker: dict | None = None
        self._approval_state: dict[str, Any] | None = None

        input_bindings = KeyBindings()
        running_bindings = KeyBindings()
        picker_bindings = KeyBindings()
        approval_bindings = KeyBindings()
        global_bindings = KeyBindings()
        not_running = Condition(lambda: not self._running)
        picker_active = Condition(lambda: self.model_picker is not None)
        approval_active = Condition(lambda: self._approval_state is not None)

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
            self._submit_approval(index=2)
            event.app.invalidate()

        def _make_approval_number_handler(index: int):
            def _handler(event: KeyPressEvent) -> None:
                self._submit_approval(index=index)
                event.app.invalidate()
            return _handler

        for _num in range(1, 4):
            approval_bindings.add(str(_num), filter=approval_active, eager=True)(
                _make_approval_number_handler(_num - 1)
            )

        # ── Running key bindings ──

        @running_bindings.add("enter", eager=True, filter=Condition(lambda: self._running and self._approval_state is None))
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")
            event.app.invalidate()

        # ── Idle key bindings ──

        @input_bindings.add("/", eager=True, filter=not_running & ~picker_active)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("/")
            event.current_buffer.start_completion(select_first=False)
            event.app.invalidate()

        @input_bindings.add("enter", eager=True, filter=not_running & ~picker_active)
        def _(event: KeyPressEvent) -> None:
            text = event.current_buffer.text.strip()
            if text:
                event.current_buffer.reset()
                self._idle_queue.put_nowait(text)

        @input_bindings.add("escape", "enter", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @input_bindings.add("c-j", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @input_bindings.add("c-d", eager=True, filter=not_running & ~picker_active)
        def _(event: KeyPressEvent) -> None:
            event.app.exit(result="exit")

        @global_bindings.add("c-c", eager=True, is_global=True)
        def _(event: KeyPressEvent) -> None:
            self._handle_ctrl_c(event)

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
        self._running = True
        self._cancel_requested = False
        self._force_exit = False
        self._app.invalidate()

    def end_running(self) -> None:
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
        }
        self.invalidate()

    def clear_approval(self) -> None:
        self._approval_state = None
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
        value = choices[index][1]
        self._approval_state = None
        if self._on_approval_response:
            self._on_approval_response(value)
        self._app.invalidate()

    def _cancel_approval(self) -> None:
        if not self._approval_state:
            return
        self._cancel_requested = True
        self._approval_state = None
        if self._on_cancel:
            self._on_cancel()
        self._app.invalidate()

    def _handle_ctrl_c(self, event: KeyPressEvent) -> None:
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
        if now - self._last_ctrl_c_time < 2.0:
            self._force_exit = True
        else:
            self._last_ctrl_c_time = now

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
        if not self._approval_state:
            return 0
        return len(self._approval_lines())

    def _render_approval(self) -> FormattedText:
        if not self._approval_state:
            return FormattedText()
        fragments = FormattedText()
        for style, text in self._approval_lines():
            fragments.append((style, text + "\n"))
        return fragments

    def _approval_lines(self) -> list[tuple[str, str]]:
        state = self._approval_state
        if not state:
            return []
        decision = state["decision"]
        command = getattr(decision, "command", None) or (getattr(decision, "tool_args", {}) or {}).get("command", "")
        approval_key = getattr(decision, "approval_key", None)
        lines: list[tuple[str, str]] = [
            ("class:running-prompt-separator", "╭─ approval " + "─" * 68),
            ("", f"  {getattr(decision, 'reason', '')}"),
            ("", ""),
            ("", f"  Tool: {getattr(decision, 'tool_name', '')}"),
            ("", f"  Risk: {getattr(getattr(decision, 'risk', ''), 'value', getattr(decision, 'risk', ''))}"),
        ]
        if command:
            lines.append(("", f"  Command: {command}"))
        lines.append(("class:running-prompt-separator", "╰" + "─" * 79))
        lines.append(("class:bottom-toolbar", "Use ↑/↓ or 1/2/3, then press Enter."))
        if approval_key:
            lines.append(("class:bottom-toolbar", f"Approval key: {approval_key}"))
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
                fragments.append(("class:bottom-toolbar", "enter: newline  |  ctrl+c: interrupt"))
        return fragments


def _box_top(columns: int) -> str:
    return "╭" + ("─" * max(columns - 2, 0)) + "╮"


def _box_bottom(columns: int) -> str:
    return "╰" + ("─" * max(columns - 2, 0)) + "╯"
