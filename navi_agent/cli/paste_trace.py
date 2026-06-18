"""Debug-only trace tool for diagnosing long-text paste splitting.

Enabled by default. Set NAVI_PASTE_TRACE=0/false/off/no to disable.
Logs JSONL to ~/.navi/paste_trace.jsonl
(overridable via NAVI_PASTE_TRACE_PATH).  Every public function swallows
all exceptions so the main flow is never affected.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import threading
from typing import Any

from ..paths import get_navi_home


_LOCK = threading.Lock()
_DISABLED_VALUES = {"0", "false", "off", "no"}


def _trace_enabled() -> bool:
    value = os.environ.get("NAVI_PASTE_TRACE", "1").strip().lower()
    return value not in _DISABLED_VALUES


def _trace_path() -> str:
    path = os.environ.get("NAVI_PASTE_TRACE_PATH")
    if path:
        return path
    return str(get_navi_home() / "paste_trace.jsonl")


def summarize_text(text: str) -> dict[str, object]:
    """Return a privacy-safe summary of *text* (no full content)."""
    newline_count = text.count("\n")
    sha12 = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    preview = text[:80].replace("\n", "\\n")
    return {
        "len": len(text),
        "newline_count": newline_count,
        "sha12": sha12,
        "preview": preview,
    }


def trace_paste(event: str, **fields: object) -> None:
    """Append one JSONL record.  Swallows all exceptions."""
    if not _trace_enabled():
        return

    record: dict[str, object] = {
        "ts": _datetime.datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
    }
    record.update(fields)

    try:
        path = _trace_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
