from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from ..paths import get_navi_home

_COLLAPSE_MIN_LINES = 8
_COLLAPSE_MIN_CHARS = 2000
_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+: \d+ lines -> (.+?)\]")


def collapse_large_paste(text: str, paste_number: int) -> tuple[str, Path | None]:
    """Collapse a large paste into a one-line placeholder, storing the content.

    Returns (placeholder_or_normalized, paste_file_or_None).
    If the text is small, returns (normalized, None) and writes nothing.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    line_count = len(normalized.splitlines())
    char_count = len(normalized)

    if not (line_count >= _COLLAPSE_MIN_LINES or char_count >= _COLLAPSE_MIN_CHARS):
        return (normalized, None)

    pastes_dir = get_navi_home() / "pastes"
    pastes_dir.mkdir(parents=True, exist_ok=True)
    paste_file = pastes_dir / f"paste_{paste_number:04d}_{datetime.now():%Y%m%d_%H%M%S}.txt"
    paste_file.write_text(normalized, encoding="utf-8")

    placeholder = f"[Pasted text #{paste_number}: {line_count} lines -> {paste_file}]"
    return (placeholder, paste_file)


def expand_paste_references(text: str) -> str:
    """Expand any paste placeholders in text back to their original content."""
    pastes_dir = (get_navi_home() / "pastes").resolve()

    def repl(match: re.Match) -> str:
        candidate = Path(match.group(1)).resolve()
        if not candidate.is_relative_to(pastes_dir):
            return match.group(0)
        try:
            return candidate.read_text(encoding="utf-8")
        except OSError:
            return match.group(0)

    return _PLACEHOLDER_RE.sub(repl, text)
