from __future__ import annotations

import io
import time
from typing import Any

from rich.columns import Columns
from rich.console import Console, ConsoleOptions, Group, RenderableType, RenderResult
from rich.live import Live
from rich.markdown import Markdown
from rich.measure import Measurement
from rich.panel import Panel
from rich.segment import Segment
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text


console = Console(highlight=False)


def render_to_ansi(renderable: RenderableType, columns: int) -> str:
    output = io.StringIO()
    render_console = Console(
        file=output,
        force_terminal=True,
        color_system=console.color_system,
        highlight=False,
        width=max(columns, 20),
    )
    render_console.print(renderable, end="")
    return output.getvalue()


def format_token_count(n: int) -> str:
    if n >= 1_000_000:
        value = n / 1_000_000
        suffix = "m"
    elif n >= 1000:
        value = n / 1000
        suffix = "k"
    else:
        return str(n)
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return f"{text}{suffix}"


def format_context_status(
    context_usage: float,
    context_tokens: int,
    max_context_tokens: int,
) -> str:
    return (
        f"context: {context_usage * 100:.1f}% "
        f"({format_token_count(context_tokens)}/{format_token_count(max_context_tokens)})"
    )


class _ShrinkToWidth:
    def __init__(self, renderable: RenderableType, max_width: int) -> None:
        self._renderable = renderable
        self._max_width = max(max_width, 1)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        width = self._resolve_width(options)
        return Measurement(0, width)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        yield from console.render(self._renderable, options.update(width=self._resolve_width(options)))

    def _resolve_width(self, options: ConsoleOptions) -> int:
        return max(1, min(self._max_width, options.max_width))


def _strip_trailing_spaces(segments: list[Segment]) -> list[Segment]:
    lines = list(Segment.split_lines(segments))
    trimmed: list[Segment] = []
    for index, line in enumerate(lines):
        line_segments = list(line)
        while line_segments:
            segment = line_segments[-1]
            if segment.control is not None:
                break
            text = segment.text.rstrip(" ")
            if text == segment.text:
                break
            if text:
                line_segments[-1] = Segment(text, segment.style, segment.control)
                break
            line_segments.pop()
        trimmed.extend(line_segments)
        if index != len(lines) - 1:
            trimmed.append(Segment.line())
    if trimmed:
        trimmed.append(Segment.line())
    return trimmed


class BulletColumns:
    def __init__(
        self,
        renderable: RenderableType,
        *,
        bullet_style: str | None = None,
        bullet: RenderableType | None = None,
        padding: int = 1,
    ) -> None:
        self._renderable = renderable
        self._bullet = bullet
        self._bullet_style = bullet_style
        self._padding = padding

    def _bullet_renderable(self) -> RenderableType:
        if self._bullet is not None:
            return self._bullet
        return Text("•", style=self._bullet_style or "")

    def _available_width(self, console: Console, options: ConsoleOptions, bullet_width: int) -> int:
        max_width = options.max_width or console.width or (bullet_width + self._padding + 1)
        return max(max_width - bullet_width - self._padding, 1)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        bullet = self._bullet_renderable()
        bullet_width = max(Measurement.get(console, options, bullet).maximum, 1)
        body = _ShrinkToWidth(self._renderable, self._available_width(console, options, bullet_width))
        return Measurement.get(console, options, Columns([bullet, body], expand=False, padding=(0, self._padding)))

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        bullet = self._bullet_renderable()
        bullet_width = max(Measurement.get(console, options, bullet).maximum, 1)
        body = _ShrinkToWidth(self._renderable, self._available_width(console, options, bullet_width))
        segments = list(console.render(Columns([bullet, body], expand=False, padding=(0, self._padding)), options))
        yield from _strip_trailing_spaces(segments)


class NaviStreamView:
    def __init__(self) -> None:
        self._live: Live | None = None
        self._content = ""
        self._reasoning_tokens = 0.0
        self._reasoning_start: float | None = None
        self._active_tool: tuple[str, dict[str, Any]] | None = None
        self._spinner = Spinner("dots", text="")
        self._raw_output_open = False

    def __enter__(self) -> NaviStreamView:
        self._live = Live(
            self._compose(),
            console=console,
            refresh_per_second=10,
            transient=True,
            vertical_overflow="visible",
        )
        self._live.start()
        return self

    def __exit__(self, *_) -> None:
        self.flush()
        if self._live is not None:
            self._live.stop()
            self._live = None

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        self._close_raw_output()
        if event_type == "assistant_delta":
            self._finish_reasoning()
            self._content += str(event.get("content") or "")
            self._refresh()
        elif event_type == "reasoning_delta":
            self._content = self._flush_content(self._content)
            if self._reasoning_start is None:
                self._reasoning_start = time.monotonic()
            self._reasoning_tokens += _estimate_tokens(str(event.get("content") or ""))
            self._refresh()
        elif event_type == "assistant_end":
            self._finish_reasoning()
            self._content = self._flush_content(self._content)
            self._refresh()
        elif event_type == "retry":
            self._finish_reasoning()
            self._content = ""
            console.print(BulletColumns(Text(str(event.get("message") or ""), style="grey50 italic")))
            console.print()
            self._refresh()
        elif event_type == "compress_error":
            console.print(BulletColumns(Text(str(event.get("message") or ""), style="yellow")))
            console.print()
            self._refresh()
        elif event_type == "tool_start":
            self._finish_reasoning()
            self._content = self._flush_content(self._content)
            self._active_tool = (str(event.get("tool_name") or "tool"), event.get("tool_args") or {})
            self._refresh()
        elif event_type == "tool_result":
            self._print_tool_result(event)
            self._active_tool = None
            self._refresh()
        elif event_type == "tool_error":
            name = str(event.get("tool_name") or "tool")
            text = Text.assemble(("Used ", ""), (name, "blue"), (" failed", "dark_red"))
            error = str(event.get("error") or "Unknown error")
            console.print(BulletColumns(text, bullet_style="dark_red"))
            console.print(BulletColumns(Text(error, style="dark_red"), bullet=Text(" ")))
            console.print()
            self._active_tool = None
            self._refresh()

    def handle_output(self, *args, **kwargs) -> None:
        self._content = self._flush_content(self._content)
        self._finish_reasoning()
        console.print(*args, **kwargs)
        end = kwargs.get("end", "\n")
        text = "".join(str(arg) for arg in args)
        self._raw_output_open = end == "" and not text.endswith(("\n", "\r"))
        self._refresh()

    def flush(self) -> None:
        self._close_raw_output()
        self._finish_reasoning()
        self._content = self._flush_content(self._content)
        self._active_tool = None
        self._refresh()

    def _close_raw_output(self) -> None:
        if self._raw_output_open:
            console.print()
            self._raw_output_open = False

    def _refresh(self) -> None:
        if self._live is not None:
            self._live.update(self._compose(), refresh=True)

    def _compose(self) -> RenderableType:
        if self._reasoning_start is not None:
            elapsed = time.monotonic() - self._reasoning_start
            tokens = int(self._reasoning_tokens)
            text = Text.assemble(
                ("Thinking", "italic"),
                ("...", "cyan"),
                (f" {elapsed:.1f}s", "grey50"),
                (f" · {format_token_count(tokens)} tokens", "grey50"),
            )
            return BulletColumns(text, bullet=self._spinner)
        if self._content:
            text = Text.assemble(("Composing...", ""), (f" {format_token_count(int(_estimate_tokens(self._content)))} tokens", "grey50"))
            return BulletColumns(text, bullet=self._spinner)
        if self._active_tool is not None:
            name, args = self._active_tool
            return BulletColumns(self._tool_headline("Using", name, args), bullet=self._spinner)
        return Text("")

    def _finish_reasoning(self) -> None:
        if self._reasoning_start is None:
            return
        elapsed = time.monotonic() - self._reasoning_start
        tokens = int(self._reasoning_tokens)
        text = Text(
            f"Thought for {elapsed:.1f}s · {format_token_count(tokens)} tokens",
            style="grey50 italic",
        )
        console.print(BulletColumns(text, bullet_style="grey50"))
        console.print()
        self._reasoning_start = None
        self._reasoning_tokens = 0.0

    def _flush_content(self, content: str) -> str:
        if not content.strip():
            return ""
        console.print(BulletColumns(Markdown(content)))
        console.print()
        return ""

    def _print_tool_result(self, event: dict[str, Any]) -> None:
        name = str(event.get("tool_name") or "tool")
        args = event.get("tool_args") or {}
        result = event.get("tool_result") or {}
        ok = bool(result.get("ok", True))
        console.print(BulletColumns(self._tool_headline("Used", name, args), bullet_style="green" if ok else "dark_red"))
        if not ok:
            console.print(BulletColumns(Text(str(result.get("error") or "Unknown error"), style="dark_red"), bullet=Text(" ")))
        elif name in {"write_file", "patch_file"}:
            path = result.get("path") or args.get("path") or ""
            added = int(result.get("added_lines") or 0)
            removed = int(result.get("removed_lines") or 0)
            console.print(
                BulletColumns(
                    Text.assemble(
                        (str(path), "grey50"),
                        (" ", "grey50"),
                        (f"+{added}", "green"),
                        (" ", "grey50"),
                        (f"-{removed}", "red"),
                    ),
                    bullet=Text(" "),
                )
            )
            diff = result.get("diff")
            if diff:
                console.print(BulletColumns(Syntax(diff, "diff", word_wrap=True), bullet=Text(" ")))
            if result.get("diff_truncated"):
                console.print(BulletColumns(Text("diff truncated", style="yellow"), bullet=Text(" ")))
        elif name == "run_command":
            exit_code = result.get("exit_code")
            style = "green" if exit_code == 0 else "dark_red"
            console.print(BulletColumns(Text(f"exit_code={exit_code}", style=style), bullet=Text(" ")))
            output = str(result.get("output") or "")
            if output.strip():
                console.print(BulletColumns(Syntax(output[-4000:], "text", word_wrap=True), bullet=Text(" ")))
        console.print()

    def _tool_headline(self, verb: str, name: str, args: dict[str, Any]) -> Text:
        text = Text()
        text.append(f"{verb} ")
        text.append(name, style="blue")
        detail = _tool_detail(name, args)
        if detail:
            text.append(" (", style="grey50")
            text.append(detail, style="grey50")
            text.append(")", style="grey50")
        return text


class NaviInlineStreamState:
    def __init__(self, on_change=None) -> None:
        self._on_change = on_change
        self._blocks: list[RenderableType] = []
        self._content = ""
        self._reasoning_tokens = 0.0
        self._reasoning_start: float | None = None
        self._composing_start: float | None = None
        self._active_tool: tuple[str, dict[str, Any]] | None = None
        self._spinner = Spinner("dots", text="")
        self._raw_output = ""

    def add_user_message(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self._append(BulletColumns(Markdown(text), bullet=Text(">", style="blue")))
        self._changed()

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        self._flush_raw_output()
        if event_type == "assistant_delta":
            self._finish_reasoning()
            was_empty = not self._content
            self._content += str(event.get("content") or "")
            if was_empty and self._content:
                self._composing_start = time.monotonic()
        elif event_type == "reasoning_delta":
            self._flush_content()
            if self._reasoning_start is None:
                self._reasoning_start = time.monotonic()
            self._reasoning_tokens += _estimate_tokens(str(event.get("content") or ""))
        elif event_type == "assistant_end":
            self._finish_reasoning()
            self._flush_content()
        elif event_type == "retry":
            self._finish_reasoning()
            self._content = ""
            self._composing_start = None
            self._append(BulletColumns(Text(str(event.get("message") or ""), style="grey50 italic")))
        elif event_type == "compress_error":
            self._append(BulletColumns(Text(str(event.get("message") or ""), style="yellow")))
        elif event_type == "tool_start":
            self._finish_reasoning()
            self._flush_content()
            self._active_tool = (str(event.get("tool_name") or "tool"), event.get("tool_args") or {})
        elif event_type == "tool_result":
            self._append_tool_result(event)
            self._active_tool = None
        elif event_type == "tool_error":
            name = str(event.get("tool_name") or "tool")
            text = Text.assemble(("Used ", ""), (name, "blue"), (" failed", "dark_red"))
            error = str(event.get("error") or "Unknown error")
            self._append(BulletColumns(text, bullet_style="dark_red"))
            self._append(BulletColumns(Text(error, style="dark_red"), bullet=Text(" ")))
            self._active_tool = None
        self._changed()

    def handle_output(self, *args, **kwargs) -> None:
        self._finish_reasoning()
        self._flush_content()
        sep = kwargs.get("sep", " ")
        end = kwargs.get("end", "\n")
        self._raw_output += sep.join(str(arg) for arg in args) + end
        if end.endswith(("\n", "\r")):
            self._flush_raw_output()
        self._changed()

    def flush(self) -> None:
        self._flush_raw_output()
        self._finish_reasoning()
        self._flush_content()
        self._active_tool = None
        self._changed()

    def render(self) -> RenderableType | None:
        blocks = list(self._blocks)
        current = self._compose_current()
        if current is not None:
            blocks.append(current)
        if not blocks:
            return None
        return Group(*blocks)

    def _compose_current(self) -> RenderableType | None:
        if self._raw_output:
            return Syntax(self._raw_output[-4000:], "text", word_wrap=True)
        if self._reasoning_start is not None:
            elapsed = time.monotonic() - self._reasoning_start
            tokens = int(self._reasoning_tokens)
            text = Text.assemble(
                ("Thinking", "italic"),
                ("...", "cyan"),
                (f" {elapsed:.1f}s", "grey50"),
                (f" · {format_token_count(tokens)} tokens", "grey50"),
            )
            return BulletColumns(text, bullet=self._spinner)
        if self._content:
            elapsed = time.monotonic() - (self._composing_start or time.monotonic())
            tokens = int(_estimate_tokens(self._content))
            text = Text.assemble(
                ("Composing", "italic"),
                ("...", "cyan"),
                (f" {elapsed:.1f}s", "grey50"),
                (f" · {format_token_count(tokens)} tokens", "grey50"),
            )
            return BulletColumns(text, bullet=self._spinner)
        if self._active_tool is not None:
            name, args = self._active_tool
            return BulletColumns(_tool_headline("Using", name, args), bullet=self._spinner)
        return None

    def _append(self, renderable: RenderableType) -> None:
        self._blocks.append(renderable)
        self._blocks.append(Text(""))

    def _finish_reasoning(self) -> None:
        if self._reasoning_start is None:
            return
        elapsed = time.monotonic() - self._reasoning_start
        tokens = int(self._reasoning_tokens)
        text = Text(
            f"Thought for {elapsed:.1f}s · {format_token_count(tokens)} tokens",
            style="grey50 italic",
        )
        self._append(BulletColumns(text, bullet_style="grey50"))
        self._reasoning_start = None
        self._reasoning_tokens = 0.0

    def _flush_content(self) -> None:
        if not self._content.strip():
            self._content = ""
            self._composing_start = None
            return
        self._append(BulletColumns(Markdown(self._content)))
        self._content = ""
        self._composing_start = None

    def _flush_raw_output(self) -> None:
        if not self._raw_output:
            return
        self._append(Syntax(self._raw_output[-4000:], "text", word_wrap=True))
        self._raw_output = ""

    def _append_tool_result(self, event: dict[str, Any]) -> None:
        name = str(event.get("tool_name") or "tool")
        args = event.get("tool_args") or {}
        result = event.get("tool_result") or {}
        ok = bool(result.get("ok", True))
        self._append(BulletColumns(_tool_headline("Used", name, args), bullet_style="green" if ok else "dark_red"))
        if not ok:
            self._append(BulletColumns(Text(str(result.get("error") or "Unknown error"), style="dark_red"), bullet=Text(" ")))
        elif name in {"write_file", "patch_file"}:
            path = result.get("path") or args.get("path") or ""
            added = int(result.get("added_lines") or 0)
            removed = int(result.get("removed_lines") or 0)
            self._append(
                BulletColumns(
                    Text.assemble(
                        (str(path), "grey50"),
                        (" ", "grey50"),
                        (f"+{added}", "green"),
                        (" ", "grey50"),
                        (f"-{removed}", "red"),
                    ),
                    bullet=Text(" "),
                )
            )
            diff = result.get("diff")
            if diff:
                self._append(BulletColumns(Syntax(diff, "diff", word_wrap=True), bullet=Text(" ")))
            if result.get("diff_truncated"):
                self._append(BulletColumns(Text("diff truncated", style="yellow"), bullet=Text(" ")))
        elif name == "run_command":
            exit_code = result.get("exit_code")
            style = "green" if exit_code == 0 else "dark_red"
            self._append(BulletColumns(Text(f"exit_code={exit_code}", style=style), bullet=Text(" ")))
            output = str(result.get("output") or "")
            if output.strip():
                self._append(BulletColumns(Syntax(output[-4000:], "text", word_wrap=True), bullet=Text(" ")))

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()


def _tool_headline(verb: str, name: str, args: dict[str, Any]) -> Text:
    text = Text()
    text.append(f"{verb} ")
    text.append(name, style="blue")
    detail = _tool_detail(name, args)
    if detail:
        text.append(" (", style="grey50")
        text.append(detail, style="grey50")
        text.append(")", style="grey50")
    return text


def _tool_detail(name: str, args: dict[str, Any]) -> str:
    if name == "run_command":
        return str(args.get("command") or "")
    if name in {"read_file", "write_file", "patch_file", "list_dir", "glob", "grep"}:
        return str(args.get("path") or args.get("pattern") or ".")
    if name == "skill_view":
        return str(args.get("name") or "")
    return ""


def _estimate_tokens(text: str) -> float:
    cjk = 0
    other = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF or 0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF:
            cjk += 1
        else:
            other += 1
    return cjk * 1.5 + other / 4


def approval_panel(decision: Any) -> Panel:
    lines = [
        Text.from_markup(f"[yellow]{decision.reason}[/yellow]"),
        Text(""),
        Text.assemble(("Tool: ", "grey50"), (decision.tool_name, "blue")),
        Text.assemble(("Risk: ", "grey50"), (decision.risk.value, "yellow")),
    ]
    if decision.command:
        lines.append(Text.assemble(("Command: ", "grey50"), decision.command))
    path = decision.tool_args.get("path")
    if path:
        lines.append(Text.assemble(("Path: ", "grey50"), str(path)))
    return Panel(
        Group(*lines),
        title="approval",
        title_align="left",
        border_style="yellow",
        padding=(0, 1),
    )
