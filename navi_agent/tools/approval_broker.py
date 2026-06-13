from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .approval import UserApprovalChoice


class ApprovalCancelled(KeyboardInterrupt):
    """Raised when approval UI is cancelled as part of turn interrupt."""


@dataclass
class _ApprovalRequest:
    response_queue: queue.Queue[UserApprovalChoice | ApprovalCancelled]


class ApprovalBroker:
    """Thread-safe bridge between runtime approval waits and the prompt UI."""

    def __init__(
        self,
        *,
        on_request: Callable[[Any], None],
        on_clear: Callable[[], None],
        default_timeout: float = 60.0,
    ) -> None:
        self._on_request = on_request
        self._on_clear = on_clear
        self._default_timeout = default_timeout
        self._lock = threading.Lock()
        self._current: _ApprovalRequest | None = None

    def request(self, decision: Any, timeout: float | None = None) -> UserApprovalChoice:
        wait_timeout = self._default_timeout if timeout is None else timeout
        request = _ApprovalRequest(
            response_queue=queue.Queue(maxsize=1),
        )

        with self._lock:
            self._current = request

        self._on_request(decision)

        deadline = time.monotonic() + wait_timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return UserApprovalChoice.REJECT
                try:
                    response = request.response_queue.get(timeout=min(0.25, remaining))
                    if isinstance(response, ApprovalCancelled):
                        raise response
                    return response
                except queue.Empty:
                    continue
        finally:
            with self._lock:
                if self._current is request:
                    self._current = None
            self._on_clear()

    def resolve(self, choice: UserApprovalChoice) -> None:
        with self._lock:
            request = self._current
        if request is None:
            return
        self._put_response(request, choice)

    def cancel_current(self) -> None:
        with self._lock:
            request = self._current
        if request is None:
            return
        self._put_response(request, ApprovalCancelled("用户中断"))

    @staticmethod
    def _put_response(
        request: _ApprovalRequest,
        choice: UserApprovalChoice | ApprovalCancelled,
    ) -> None:
        try:
            request.response_queue.put_nowait(choice)
        except queue.Full:
            pass
