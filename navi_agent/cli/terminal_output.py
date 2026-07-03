from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text


class TerminalOutput:
    """Semantic scrollback output boundary for the interactive CLI."""

    def __init__(
        self,
        print_fn: Callable[..., None],
        event_printer: Callable[..., None],
    ) -> None:
        self._print = print_fn
        self._event_printer = event_printer

    def raw(self, *args: Any, **kwargs: Any) -> None:
        self._print(*args, **kwargs)

    def notice(self, message: str) -> None:
        self._print(message)

    def error(self, message: str) -> None:
        self._print(f"[red]{message}[/red]")

    def user_message(self, text: str, image_paths: list[Path]) -> None:
        lines = [f"  [image] {path}" for path in image_paths]
        if text:
            lines.append(f"> {text}")
        elif not lines:
            lines.append(">")
        self._print(Text("\n" + "\n".join(lines) + "\n", style="#87CEEB"))

    def assistant(self, answer: str) -> None:
        if answer:
            self._print(Group(Text(""), Markdown(answer)))

    def agent_event(self, event: dict[str, Any], *, box: Any = None) -> None:
        self._event_printer(event, box=box)
