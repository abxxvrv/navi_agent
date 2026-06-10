"""Per-thread interrupt signaling for all tools.

Provides thread-scoped interrupt tracking so that interrupting one agent
session does not kill tools running in other sessions.

Usage in tools:
    from .interrupt import is_interrupted
    if is_interrupted():
        return {"error": "[interrupted]"}
"""

import threading

# Set of thread idents that have been interrupted.
_interrupted_threads: set[int] = set()
_lock = threading.Lock()


def set_interrupt(active: bool, thread_id: int | None = None) -> None:
    """Set or clear interrupt for a specific thread."""
    tid = thread_id if thread_id is not None else threading.get_ident()
    with _lock:
        if active:
            _interrupted_threads.add(tid)
        else:
            _interrupted_threads.discard(tid)


def is_interrupted() -> bool:
    """Check if an interrupt has been requested for the current thread."""
    tid = threading.get_ident()
    with _lock:
        return tid in _interrupted_threads


def clear_all() -> None:
    """Clear all interrupt flags (e.g., at turn boundary)."""
    with _lock:
        _interrupted_threads.clear()
