from __future__ import annotations

import datetime as _datetime
import json
import os
import threading
from pathlib import Path
from typing import Any

from ..paths import get_navi_home


_LOCK = threading.Lock()
_DISABLED_VALUES = {"0", "false", "off", "no"}


def interrupt_trace_enabled() -> bool:
    value = os.environ.get("NAVI_INTERRUPT_TRACE", "1").strip().lower()
    return value not in _DISABLED_VALUES


def interrupt_trace_path() -> Path:
    path = os.environ.get("NAVI_INTERRUPT_TRACE_PATH")
    if path:
        return Path(path).expanduser().resolve()
    return get_navi_home() / "interrupt_trace.log"


def trace_interrupt(source: str, **fields: Any) -> None:
    if not interrupt_trace_enabled():
        return

    record: dict[str, Any] = {
        "ts": _datetime.datetime.now().isoformat(timespec="milliseconds"),
        "source": source,
        "pid": os.getpid(),
        "thread": threading.current_thread().name,
    }
    record.update(fields)

    try:
        path = interrupt_trace_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _LOCK:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except Exception:
        pass
