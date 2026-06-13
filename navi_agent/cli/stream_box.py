"""Streaming output box — Hermes-style bordered reasoning/response display.

Uses raw ANSI + prompt_toolkit's print_formatted_text to safely print
through patch_stdout. No Rich, no Live, no conflicts with prompt_toolkit.
"""

from __future__ import annotations

import re
import shutil
from typing import Any

from .markdown_tables import (
    is_table_divider,
    looks_like_table_row,
    realign_markdown_tables,
)

# ANSI constants
_RST = "\033[0m"
_DIM = "\033[2;3m"      # dim + italic
_ACCENT = "\033[38;2;255;215;0m"  # gold
_PAD = "    "           # 4-space indent for response content


def _box_width() -> int:
    try:
        return max(32, shutil.get_terminal_size((80, 24)).columns)
    except Exception:
        return 80


def _strip_markdown_syntax(text: str) -> str:
    """Best-effort markdown marker removal for plain-text display."""
    if not text:
        return text
    plain = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    plain = re.sub(r"(```+|~~~+)", "", plain)
    plain = re.sub(r"`([^`]*)`", r"\1", plain)
    plain = re.sub(r"!\[([^\]]*)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", plain)
    plain = re.sub(r"\*\*\*([^*]+)\*\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)___([^_]+)___(?!\w)", r"\1", plain)
    plain = re.sub(r"\*\*([^*]+)\*\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)__([^_]+)__(?!\w)", r"\1", plain)
    plain = re.sub(r"\*([^\s*][^*]*?[^\s*])\*", r"\1", plain)
    plain = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", plain)
    plain = re.sub(r"~~([^~]+)~~", r"\1", plain)
    plain = re.sub(r"^\s{0,3}(?:[-_]\s*){3,}$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"^\s{0,3}(?:\*\s*){3}\s*$", "", plain, flags=re.MULTILINE)
    plain = re.sub(r"\n{3,}", "\n\n", plain)
    return plain.strip("\n")


class StreamingBox:
    """Manages reasoning and response box state for streaming output."""

    def __init__(self, print_fn) -> None:
        self._print = print_fn
        self._reasoning_open = False
        self._response_open = False
        self._reasoning_buf = ""
        self._response_buf = ""
        self._had_output = False
        self._in_code_block = False
        self._in_table = False
        self._table_buf: list[str] = []

    def reset(self) -> None:
        """Reset state for a new turn."""
        self._had_output = False
        self._in_code_block = False
        self._in_table = False
        self._table_buf = []

    @property
    def had_output(self) -> bool:
        return self._had_output

    # ── Reasoning ──────────────────────────────────────────────────

    def reasoning_delta(self, text: str) -> None:
        """Stream a reasoning token into the dim box."""
        if not text:
            return
        if self._response_open:
            self.close_response()

        if not self._reasoning_open:
            self._reasoning_open = True
            self._had_output = True
            w = _box_width()
            label = " Reasoning "
            fill = w - 2 - len(label)
            self._print(f"\n{_DIM}┌─{label}{'─' * max(fill - 1, 0)}┐{_RST}")

        self._reasoning_buf += text

        while "\n" in self._reasoning_buf:
            line, self._reasoning_buf = self._reasoning_buf.split("\n", 1)
            self._print(f"{_DIM}  {line}{_RST}")

        if len(self._reasoning_buf) > 80:
            self._print(f"{_DIM}  {self._reasoning_buf}{_RST}")
            self._reasoning_buf = ""

    def close_reasoning(self) -> None:
        """Close the reasoning box if open."""
        if not self._reasoning_open:
            return
        if self._reasoning_buf:
            self._print(f"{_DIM}  {self._reasoning_buf}{_RST}")
            self._reasoning_buf = ""
        w = _box_width()
        self._print(f"{_DIM}└{'─' * (w - 2)}┘{_RST}")
        self._reasoning_open = False

    # ── Response ───────────────────────────────────────────────────

    def response_delta(self, text: str) -> None:
        """Stream a response token into the bordered box."""
        if not text:
            return
        if self._reasoning_open:
            self.close_reasoning()

        if not self._response_open:
            self._response_open = True
            self._had_output = True
            text = text.lstrip("\n")
            if not text:
                return
            w = _box_width()
            label = " Navi "
            fill = w - 2 - len(label)
            self._print(f"\n{_ACCENT}╭─{label}{'─' * max(fill - 1, 0)}╮{_RST}")

        self._response_buf += text

        while "\n" in self._response_buf:
            line, self._response_buf = self._response_buf.split("\n", 1)
            self._emit_response_line(line)

    def _emit_response_line(self, line: str) -> None:
        """Print a single response line with markdown stripping and table handling."""
        stripped = line.strip()

        # Code block fence tracking
        if stripped.startswith("```") or stripped.startswith("~~~"):
            self._in_code_block = not self._in_code_block
            # Flush any pending table before entering code block
            if self._in_code_block and self._in_table:
                self._flush_table()
            return

        if self._in_code_block:
            self._print(f"{_PAD}{line}")
            return

        # Table row detection (outside code blocks)
        if looks_like_table_row(line) or is_table_divider(line):
            if not self._in_table:
                self._in_table = True
                self._table_buf = []
            self._table_buf.append(line)
            return

        # Non-table line: flush any pending table first
        if self._in_table:
            self._flush_table()

        # Normal line: strip markdown and print (preserve blank lines like Hermes)
        clean = _strip_markdown_syntax(line)
        self._print(f"{_PAD}{clean}")

    def _flush_table(self) -> None:
        """Realign and print buffered table rows."""
        if not self._table_buf:
            return
        joined = "\n".join(self._table_buf)
        # available width = terminal width minus box borders minus indent
        avail = _box_width() - 2 - len(_PAD)
        block = realign_markdown_tables(joined, avail)
        for ln in block.split("\n"):
            clean = _strip_markdown_syntax(ln)
            if clean:
                self._print(f"{_PAD}{clean}")
        self._table_buf = []
        self._in_table = False

    def close_response(self) -> None:
        """Close the response box if open."""
        if not self._response_open:
            return
        # Flush pending table
        if self._in_table:
            self._flush_table()
        # Flush remaining buffer
        if self._response_buf:
            self._emit_response_line(self._response_buf)
            self._response_buf = ""
        w = _box_width()
        self._print(f"{_ACCENT}╰{'─' * (w - 2)}╯{_RST}")
        self._response_open = False
        self._in_code_block = False
        self._in_table = False
        self._table_buf = []

    def close_all(self) -> None:
        """Close any open boxes."""
        self.close_reasoning()
        self.close_response()

    @property
    def has_output(self) -> bool:
        """True if any box is currently open."""
        return self._reasoning_open or self._response_open
