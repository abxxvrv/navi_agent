from __future__ import annotations

import threading

import pytest

from navi_agent.runtime.monitor import EVENT_LIMIT, MAX_TIMEOUT_MS, Monitor


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class _TaskManager:
    def __init__(self) -> None:
        self.calls = []
        self.killed = []

    def start_command(self, command, cwd, **kwargs):
        self.calls.append((command, cwd, kwargs))
        return {"task_id": "task-1"}

    def kill(self, task_id):
        self.killed.append(task_id)
        return {"task_id": task_id, "outcome": "killed"}


def test_start_uses_background_monitor_task_and_default_timeout():
    manager = _TaskManager()
    monitor = Monitor(manager, lambda _event: None)

    result = monitor.start("tail -f app.log", "watch app", ".", "/bin/bash")

    command, cwd, args = manager.calls[0]
    assert result == {
        "task_id": "task-1",
        "timeout_ms": MAX_TIMEOUT_MS,
        "persistent": False,
    }
    assert (command, cwd) == ("tail -f app.log", ".")
    assert args["background"] is True
    assert args["timeout_seconds"] == MAX_TIMEOUT_MS / 1000
    assert args["description"] == "watch app"
    assert args["task_type"] == "monitor"
    assert callable(args["on_line"])
    assert callable(args["on_done"])


def test_persistent_monitor_has_no_deadline():
    manager = _TaskManager()
    monitor = Monitor(manager, lambda _event: None)

    result = monitor.start(
        "tail -f app.log",
        "watch app",
        ".",
        "/bin/bash",
        timeout_ms=MAX_TIMEOUT_MS + 1,
        persistent=True,
    )

    assert result == {"task_id": "task-1", "timeout_ms": 0, "persistent": True}
    assert manager.calls[0][2]["timeout_seconds"] is None


def test_non_persistent_timeout_must_be_within_limit():
    monitor = Monitor(_TaskManager(), lambda _event: None)

    with pytest.raises(ValueError, match=str(MAX_TIMEOUT_MS)):
        monitor.start("cmd", "desc", ".", "/bin/bash", timeout_ms=MAX_TIMEOUT_MS + 1)
    with pytest.raises(ValueError, match="positive"):
        monitor.start("cmd", "desc", ".", "/bin/bash", timeout_ms=0)


def test_lines_are_truncated_batched_and_event_is_bounded():
    manager = _TaskManager()
    clock = _Clock()
    events = []
    monitor = Monitor(manager, events.append, clock=clock)

    for _ in range(7):
        monitor._on_line("task-1", "watch app", "x" * 600)
    monitor._flush("task-1")

    assert len(events) == 1
    assert events[0]["type"] == "monitor_event"
    assert events[0]["task_id"] == "task-1"
    assert events[0]["description"] == "watch app"
    assert events[0]["output"].startswith("x" * 500 + "\n")
    assert len(events[0]["output"]) == 3000


def test_lines_are_automatically_batched_for_200ms():
    manager = _TaskManager()
    events = []
    emitted = threading.Event()

    def on_event(event):
        events.append(event)
        emitted.set()

    monitor = Monitor(manager, on_event)
    monitor._on_line("task-1", "watch app", "first")
    monitor._on_line("task-1", "watch app", "second")

    assert emitted.wait(1)
    assert [event["output"] for event in events] == ["first\nsecond"]


def test_monitor_flushes_and_releases_state_when_command_finishes():
    manager = _TaskManager()
    events = []
    monitor = Monitor(manager, events.append)
    monitor.start("printf done", "watch app", ".", "/bin/bash")
    callbacks = manager.calls[0][2]

    callbacks["on_line"]("task-1", "watch app", "done")
    callbacks["on_done"]("task-1")

    assert [event["output"] for event in events] == ["done"]
    assert "task-1" not in monitor._states


def test_pending_batch_memory_is_bounded():
    manager = _TaskManager()
    monitor = Monitor(manager, lambda _event: None)

    for _ in range(10_000):
        monitor._on_line("task-1", "watch app", "x" * 600)

    state = monitor._states["task-1"]
    assert state.buffered_chars <= EVENT_LIMIT + 1
    assert sum(len(line) for line in state.lines) <= EVENT_LIMIT
    monitor._finish("task-1")


def test_token_bucket_starts_with_ten_and_refills_one_every_two_seconds():
    manager = _TaskManager()
    clock = _Clock()
    events = []
    monitor = Monitor(manager, events.append, clock=clock)

    for index in range(11):
        monitor._on_line("task-1", "watch app", str(index))
        monitor._flush("task-1")
    assert len(events) == 10

    clock.now = 2.0
    monitor._on_line("task-1", "watch app", "refilled")
    monitor._flush("task-1")
    assert "1 events suppressed" in events[-1]["output"]
    assert events[-1]["output"].endswith("refilled")
    assert len(events) == 11


def test_continuous_overload_for_thirty_seconds_kills_monitor():
    manager = _TaskManager()
    clock = _Clock()
    events = []
    monitor = Monitor(manager, events.append, clock=clock)

    for _ in range(11):
        monitor._on_line("task-1", "watch app", "line")
        monitor._flush("task-1")

    for second in range(2, 32, 2):
        clock.now = float(second)
        for _ in range(2):
            monitor._on_line("task-1", "watch app", "line")
            monitor._flush("task-1")

    assert manager.killed == ["task-1"]
    assert events[-1]["task_id"] == "task-1"
    assert "Monitor stopped" in events[-1]["output"]
