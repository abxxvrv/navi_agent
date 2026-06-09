"""Minimal prompt_toolkit input box — Hermes-style.

All conversation content is printed directly to stdout via Rich console.
prompt_toolkit only manages the input area at the bottom of the terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import queue
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app_or_none
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent, merge_key_bindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, VSplit, Window, Dimension
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.styles import Style as PTStyle


_NAVI_STYLE = PTStyle.from_dict({
    "bottom-toolbar": "bg:#1a1a2e #C0C0C0",
    "running-prompt-separator": "#888888",
    "input": "",
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
        input: Any = None,
        output: Any = None,
    ) -> None:
        self._running = False
        self._queued: list[str] = []
        self._bottom_toolbar = bottom_toolbar
        self._custom_io = input is not None or output is not None
        self._idle_queue: asyncio.Queue[str] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cancel_requested = False

        input_bindings = KeyBindings()
        running_bindings = KeyBindings()
        not_running = Condition(lambda: not self._running)

        # Running Enter: queue text
        @running_bindings.add("enter", eager=True, filter=Condition(lambda: self._running))
        def _(event: KeyPressEvent) -> None:
            text = event.current_buffer.text.strip()
            if text:
                self._queued.append(text)
                event.current_buffer.set_document(Document(), bypass_readonly=True)
            event.app.invalidate()

        # Running Ctrl+C: request cancel
        @running_bindings.add("c-c", eager=True, filter=Condition(lambda: self._running))
        def _(event: KeyPressEvent) -> None:
            self._cancel_requested = True
            event.app.invalidate()

        # Idle Enter: submit to message loop
        @input_bindings.add("enter", eager=True, filter=not_running)
        def _(event: KeyPressEvent) -> None:
            text = event.current_buffer.text.strip()
            if text:
                event.current_buffer.reset()
                self._idle_queue.put_nowait(text)

        # Shift+Enter / Alt+Enter / Ctrl+J: newline
        @input_bindings.add("escape", "enter", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        @input_bindings.add("c-j", eager=True)
        def _(event: KeyPressEvent) -> None:
            event.current_buffer.insert_text("\n")

        # Ctrl+C / Ctrl+D: exit
        @input_bindings.add("c-c", eager=True, filter=not_running)
        def _(event: KeyPressEvent) -> None:
            event.app.exit(result="exit")

        @input_bindings.add("c-d", eager=True, filter=not_running)
        def _(event: KeyPressEvent) -> None:
            event.app.exit(result="exit")

        self._buffer = Buffer(
            history=FileHistory(str(history_path)),
            auto_suggest=AutoSuggestFromHistory(),
            completer=completer,
            complete_while_typing=False,
        )

        self._layout = Layout(
            HSplit([
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
            ])
        )

        self._app: Application[str] = Application(
            layout=self._layout,
            key_bindings=merge_key_bindings([running_bindings, input_bindings, key_bindings]),
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
        self._queued = []
        self._cancel_requested = False
        self._app.invalidate()

    def end_running(self) -> None:
        self._running = False
        self._app.invalidate()

    def take_queued(self) -> list[str]:
        result = list(self._queued)
        self._queued = []
        return result

    @property
    def cancel_requested(self) -> bool:
        return self._cancel_requested

    def invalidate(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._app.invalidate)
        else:
            self._app.invalidate()

    @property
    def is_running(self) -> bool:
        return self._running

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
            fragments.append(("class:bottom-toolbar", "enter: queue  |  ctrl+c: interrupt"))
        return fragments


def _box_top(columns: int) -> str:
    return "╭" + ("─" * max(columns - 2, 0)) + "╮"


def _box_bottom(columns: int) -> str:
    return "╰" + ("─" * max(columns - 2, 0)) + "╯"
