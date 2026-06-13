"""CJK/wide-character-aware re-alignment of markdown tables.

Ported from Hermes's agent/markdown_tables.py.
Falls back to len() when wcwidth is not installed.
"""

from __future__ import annotations

import re
from typing import List

try:
    from wcwidth import wcswidth as _wcswidth

    def _disp_width(s: str) -> int:
        w = _wcswidth(s)
        return w if w > 0 else 0
except ImportError:
    def _disp_width(s: str) -> int:
        return len(s)


_DIVIDER_CELL_RE = re.compile(r"^\s*:?-{3,}:?\s*$")
_MIN_COL_WIDTH = 3


def _pad_to_width(s: str, target: int) -> str:
    return s + " " * max(0, target - _disp_width(s))


def split_table_row(row: str) -> List[str]:
    """Split ``| a | b | c |`` into ``["a", "b", "c"]`` with trims."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def is_table_divider(row: str) -> bool:
    """True when ``row`` is a markdown table separator line."""
    cells = split_table_row(row)
    return len(cells) > 1 and all(_DIVIDER_CELL_RE.match(c) for c in cells)


def looks_like_table_row(row: str) -> bool:
    """True when ``row`` could plausibly be a markdown table row."""
    if "|" not in row:
        return False
    stripped = row.strip()
    if not stripped:
        return False
    if stripped.startswith("|"):
        return True
    return stripped.count("|") >= 2


def _render_block(rows: List[List[str]], available_width: int | None = None) -> List[str]:
    """Render rows (header + body, divider implied) at uniform widths.

    If available_width is given and the table would exceed it, falls back
    to vertical key-value rendering.
    """
    ncols = max(len(r) for r in rows)
    rows = [r + [""] * (ncols - len(r)) for r in rows]

    widths = [
        max(_MIN_COL_WIDTH, *(_disp_width(r[c]) for r in rows))
        for c in range(ncols)
    ]

    horizontal_width = sum(widths) + 3 * ncols + 1

    if available_width is not None and horizontal_width > max(available_width, 20):
        return _render_vertical(rows, ncols, available_width)

    def _row(cells: List[str]) -> str:
        return (
            "| "
            + " | ".join(_pad_to_width(c, widths[k]) for k, c in enumerate(cells))
            + " |"
        )

    out = [_row(rows[0])]
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for r in rows[1:]:
        out.append(_row(r))
    return out


def _render_vertical(
    rows: List[List[str]], ncols: int, available_width: int
) -> List[str]:
    """Render a too-wide table as vertical Header: value rows."""
    if not rows:
        return []

    headers = rows[0] + [""] * (ncols - len(rows[0]))
    body = rows[1:]

    labels = [h or f"Column {i + 1}" for i, h in enumerate(headers)]

    sep_width = max(20, min(40, available_width - 2)) if available_width else 30
    separator = "─" * sep_width
    indent = "  "

    out: List[str] = []
    for ri, row in enumerate(body):
        if ri > 0:
            out.append(separator)
        for ci in range(ncols):
            label = labels[ci]
            value = row[ci] if ci < len(row) else ""
            if not value:
                out.append(f"{label}:")
                continue
            label_w = _disp_width(label)
            budget = max(10, available_width - label_w - 2)
            wrapped = _wrap_to_width(value, budget)
            out.append(f"{label}: {wrapped[0]}")
            if len(wrapped) > 1:
                cont_budget = max(10, available_width - _disp_width(indent))
                cont_text = " ".join(wrapped[1:])
                for cl in _wrap_to_width(cont_text, cont_budget):
                    if cl.strip():
                        out.append(f"{indent}{cl}")
    return out


def _wrap_to_width(text: str, width: int) -> List[str]:
    """Soft-wrap text at word boundaries to fit width display cells."""
    if width <= 0 or not text:
        return [text]

    words = text.split()
    if not words:
        return [""]

    lines: List[str] = []
    current = ""
    current_w = 0

    for word in words:
        ww = _disp_width(word)
        if not current:
            if ww <= width:
                current = word
                current_w = ww
            else:
                # Hard break long word
                buf = ""
                bw = 0
                for ch in word:
                    cw = _disp_width(ch) or 1
                    if bw + cw > width and buf:
                        lines.append(buf)
                        buf = ch
                        bw = cw
                    else:
                        buf += ch
                        bw += cw
                if buf:
                    current = buf
                    current_w = _disp_width(current)
            continue
        if current_w + 1 + ww <= width:
            current += " " + word
            current_w += 1 + ww
        else:
            lines.append(current)
            if ww <= width:
                current = word
                current_w = ww
            else:
                current = ""
                current_w = 0
    if current:
        lines.append(current)
    return lines or [""]


def realign_markdown_tables(text: str, available_width: int | None = None) -> str:
    """Rewrite every | ... | + divider block with width-aware padding.

    Lines not part of a table are returned verbatim.
    """
    if "|" not in text:
        return text

    lines = text.split("\n")
    out: List[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        if (
            "|" in line
            and i + 1 < n
            and is_table_divider(lines[i + 1])
        ):
            header = split_table_row(line)
            body: List[List[str]] = []
            j = i + 2
            while j < n and "|" in lines[j] and lines[j].strip():
                if is_table_divider(lines[j]):
                    j += 1
                    continue
                body.append(split_table_row(lines[j]))
                j += 1

            if any(c for c in header) or body:
                out.extend(_render_block([header] + body, available_width))
                i = j
                continue
        out.append(line)
        i += 1

    return "\n".join(out)
