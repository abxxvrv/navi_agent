from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .interrupt import clear_all, is_interrupted, set_interrupt


class TurnScope:
    """Interrupt state and active resources for one agent turn."""

    def __init__(self, cancel_event: threading.Event | None = None) -> None:
        self.cancel_event = cancel_event or threading.Event()
        self.execution_thread_id: int | None = None
        self._tool_worker_thread_ids: set[int] = set()
        self._aborters: list[Callable[[], None]] = []
        self._approval_cancellers: list[Callable[[], None]] = []
        self._lock = threading.Lock()

    def reset(self) -> None:
        self.cancel_event.clear()
        clear_all()
        with self._lock:
            self.execution_thread_id = None
            self._tool_worker_thread_ids.clear()
            self._aborters.clear()
            self._approval_cancellers.clear()

    def attach_execution_thread(self, thread_id: int | None = None) -> None:
        with self._lock:
            self.execution_thread_id = thread_id or threading.get_ident()

    def cancel(self, _reason: str | None = None) -> None:
        self.cancel_event.set()
        with self._lock:
            execution_thread_id = self.execution_thread_id
            worker_thread_ids = list(self._tool_worker_thread_ids)
            aborters = list(self._aborters)
            approval_cancellers = list(self._approval_cancellers)

        if execution_thread_id is not None:
            set_interrupt(True, execution_thread_id)
        for thread_id in worker_thread_ids:
            set_interrupt(True, thread_id)
        for cancel_approval in approval_cancellers:
            _call_safely(cancel_approval)
        for abort in aborters:
            _call_safely(abort)

    def is_cancelled(self) -> bool:
        return self.cancel_event.is_set() or is_interrupted()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise KeyboardInterrupt("用户中断")

    @contextmanager
    def tool_worker(self) -> Iterator[None]:
        thread_id = threading.get_ident()
        with self._lock:
            self._tool_worker_thread_ids.add(thread_id)
        if self.cancel_event.is_set():
            set_interrupt(True, thread_id)
        try:
            yield
        finally:
            with self._lock:
                self._tool_worker_thread_ids.discard(thread_id)
            set_interrupt(False, thread_id)

    @contextmanager
    def aborter(self, abort: Callable[[], None]) -> Iterator[None]:
        with self._lock:
            self._aborters.append(abort)
        try:
            yield
        finally:
            with self._lock:
                try:
                    self._aborters.remove(abort)
                except ValueError:
                    pass

    @contextmanager
    def approval_canceller(self, cancel: Callable[[], None] | None) -> Iterator[None]:
        if cancel is None:
            yield
            return

        with self._lock:
            self._approval_cancellers.append(cancel)
        try:
            yield
        finally:
            with self._lock:
                try:
                    self._approval_cancellers.remove(cancel)
                except ValueError:
                    pass

    def close(self) -> None:
        with self._lock:
            worker_thread_ids = list(self._tool_worker_thread_ids)
            execution_thread_id = self.execution_thread_id
            self._tool_worker_thread_ids.clear()
            self._aborters.clear()
            self._approval_cancellers.clear()
            self.execution_thread_id = None

        if execution_thread_id is not None:
            set_interrupt(False, execution_thread_id)
        for thread_id in worker_thread_ids:
            set_interrupt(False, thread_id)

    @property
    def tool_worker_thread_ids(self) -> set[int]:
        with self._lock:
            return set(self._tool_worker_thread_ids)


def _call_safely(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        pass
