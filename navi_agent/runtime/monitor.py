from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .task_manager import TaskManager


MAX_TIMEOUT_MS = 36_000_000
LINE_LIMIT = 500
EVENT_LIMIT = 3_000
BATCH_SECONDS = 0.2
TOKEN_CAPACITY = 10
TOKEN_REFILL_SECONDS = 2.0
OVERLOAD_SECONDS = 30.0


@dataclass
class _MonitorState:
    description: str
    tokens: int
    last_refill: float
    lines: list[str] = field(default_factory=list)
    buffered_chars: int = 0
    timer: threading.Timer | None = None
    suppressed_count: int = 0
    suppression_start: float | None = None
    last_suppression: float | None = None
    killed: bool = False


class Monitor:
    def __init__(
        self,
        task_manager: TaskManager,
        on_event: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.task_manager = task_manager
        self.on_event = on_event
        self.clock = clock
        self._states: dict[str, _MonitorState] = {}
        self._lock = threading.Lock()

    def start(
        self,
        command: str,
        description: str,
        cwd: str,
        shell_path: str,
        shell: str = "bash",
        timeout_ms: int | None = None,
        persistent: bool = False,
    ) -> dict[str, Any]:
        if persistent:
            resolved_timeout = 0
            timeout_seconds = None
        else:
            resolved_timeout = MAX_TIMEOUT_MS if timeout_ms is None else timeout_ms
            if resolved_timeout <= 0:
                raise ValueError("timeout_ms must be positive")
            if resolved_timeout > MAX_TIMEOUT_MS:
                raise ValueError(f"timeout_ms must not exceed {MAX_TIMEOUT_MS}")
            timeout_seconds = resolved_timeout / 1000

        task = self.task_manager.start_command(
            command,
            cwd,
            shell_path=shell_path,
            shell=shell,
            background=True,
            timeout_seconds=timeout_seconds,
            description=description,
            task_type="monitor",
            on_line=self._on_line,
            on_done=self._finish,
        )
        return {
            "task_id": task["task_id"],
            "timeout_ms": resolved_timeout,
            "persistent": persistent,
        }

    def _on_line(self, task_id: str, description: str, line: str) -> None:
        line = line.strip()
        if not line:
            return

        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                state = _MonitorState(
                    description=description,
                    tokens=TOKEN_CAPACITY,
                    last_refill=self.clock(),
                )
                self._states[task_id] = state
            if state.killed:
                return
            remaining = EVENT_LIMIT - state.buffered_chars
            if remaining > 0:
                buffered = line[: min(LINE_LIMIT, remaining)]
                state.lines.append(buffered)
                state.buffered_chars += len(buffered) + 1
            if state.timer is None:
                state.timer = threading.Timer(BATCH_SECONDS, self._flush, args=(task_id,))
                state.timer.daemon = True
                state.timer.start()

    def _flush(self, task_id: str) -> None:
        event = None
        should_kill = False
        with self._lock:
            state = self._states.get(task_id)
            if state is None:
                return
            if state.timer is not None:
                state.timer.cancel()
                state.timer = None
            if not state.lines or state.killed:
                return

            output = "\n".join(state.lines)[:EVENT_LIMIT]
            state.lines.clear()
            state.buffered_chars = 0
            now = self.clock()
            elapsed = now - state.last_refill
            refills = int(elapsed // TOKEN_REFILL_SECONDS)
            if refills:
                state.tokens = min(TOKEN_CAPACITY, state.tokens + refills)
                state.last_refill += refills * TOKEN_REFILL_SECONDS

            if state.tokens:
                state.tokens -= 1
                if (
                    state.last_suppression is not None
                    and now - state.last_suppression > TOKEN_REFILL_SECONDS * 3
                ):
                    state.suppression_start = None
                    state.last_suppression = None
                if state.suppressed_count:
                    output = (
                        f"[{state.suppressed_count} events suppressed -- output rate too high. "
                        "Restart this monitor with a more selective filter.]\n"
                        f"{output}"
                    )[:EVENT_LIMIT]
                    state.suppressed_count = 0
                event = {
                    "type": "monitor_event",
                    "task_id": task_id,
                    "description": state.description,
                    "output": output,
                }
            else:
                state.suppressed_count += 1
                state.last_suppression = now
                if state.suppression_start is None:
                    state.suppression_start = now
                elif now - state.suppression_start >= OVERLOAD_SECONDS:
                    state.killed = True
                    should_kill = True
                    event = {
                        "type": "monitor_event",
                        "task_id": task_id,
                        "description": state.description,
                        "output": (
                            "[Monitor stopped -- the command produced too much output "
                            f"({state.suppressed_count} events suppressed). Restart it "
                            "with a more selective filter.]"
                        ),
                    }

        if event is not None:
            self.on_event(event)
        if should_kill:
            self.task_manager.kill(task_id)

    def _finish(self, task_id: str) -> None:
        self._flush(task_id)
        with self._lock:
            state = self._states.pop(task_id, None)
            if state is not None and state.timer is not None:
                state.timer.cancel()
