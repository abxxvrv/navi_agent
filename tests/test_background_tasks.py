from __future__ import annotations

import os
import shlex
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from navi_agent.runtime.task_manager import TaskManager, _Task


def _python_command(source: str) -> str:
    return f"{shlex.quote(sys.executable)} -u -c {shlex.quote(source)}"


def _start(
    manager: TaskManager,
    tmp_path: Path,
    source: str,
    *,
    background: bool = True,
) -> dict:
    return manager.start_command(
        _python_command(source),
        tmp_path,
        shell_path="/bin/bash",
        shell="bash",
        background=background,
        timeout_seconds=None,
        encoding="utf-8",
    )


def test_explicit_background_returns_running_snapshot_and_final_output(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    try:
        started = _start(
            manager,
            tmp_path,
            "import time; print('ready', flush=True); time.sleep(0.25); print('done')",
        )

        assert started["task_type"] == "command"
        assert started["status"] == "running"
        assert started["pid"] > 0
        assert Path(started["output_file"]).parent == tmp_path / "logs"

        finished = manager.get_output([started["task_id"]], timeout_ms=3000)[0]

        assert finished["status"] == "completed"
        assert finished["exit_code"] == 0
        assert finished["output"] == "ready\ndone\n"
        assert Path(finished["output_file"]).read_text(encoding="utf-8") == finished["output"]
    finally:
        manager.shutdown()


def test_poll_is_non_blocking_and_wait_all_preserves_input_order(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    try:
        slow = _start(
            manager,
            tmp_path,
            "import time; time.sleep(0.6); print('slow')",
        )
        fast = _start(
            manager,
            tmp_path,
            "import time; time.sleep(0.1); print('fast')",
        )
        task_ids = [slow["task_id"], fast["task_id"]]

        before = time.monotonic()
        polled = manager.get_output(task_ids, timeout_ms=0)
        elapsed = time.monotonic() - before

        assert elapsed < 0.25
        assert [task["task_id"] for task in polled] == task_ids
        assert slow["task_id"] in {
            task["task_id"] for task in polled if task["status"] == "running"
        }

        waited = manager.wait_tasks(task_ids, mode="wait_all", timeout_ms=3000)

        assert [task["task_id"] for task in waited] == task_ids
        assert [task["status"] for task in waited] == ["completed", "completed"]
        assert [task["output"] for task in waited] == ["slow\n", "fast\n"]
    finally:
        manager.shutdown()


def test_wait_any_returns_when_first_task_finishes(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    try:
        fast = _start(
            manager,
            tmp_path,
            "import time; time.sleep(0.05); print('fast')",
        )
        slow = _start(
            manager,
            tmp_path,
            "import time; time.sleep(1); print('slow')",
        )
        task_ids = [fast["task_id"], slow["task_id"]]

        waited = manager.wait_tasks(task_ids, mode="wait_any", timeout_ms=2000)
        by_id = {task["task_id"]: task for task in waited}

        assert by_id[fast["task_id"]]["status"] == "completed"
        assert by_id[slow["task_id"]]["status"] == "running"
    finally:
        manager.shutdown()


def test_wait_any_ignores_unknown_ids_and_zero_timeout_only_polls(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    try:
        started = _start(
            manager,
            tmp_path,
            "import time; time.sleep(0.2); print('done')",
        )

        before = time.monotonic()
        polled = manager.wait_tasks(
            ["missing", started["task_id"]],
            mode="wait_any",
            timeout_ms=0,
        )
        assert time.monotonic() - before < 0.1
        assert polled[1]["status"] == "running"

        waited = manager.wait_tasks(
            ["missing", started["task_id"]],
            mode="wait_any",
            timeout_ms=2000,
        )
        assert waited[0]["status"] == "not_found"
        assert waited[1]["status"] == "completed"
    finally:
        manager.shutdown()


def test_task_queries_reject_more_than_twenty_ids(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    task_ids = [f"task-{index}" for index in range(21)]
    try:
        with pytest.raises(ValueError, match="20"):
            manager.get_output(task_ids)
        with pytest.raises(ValueError, match="20"):
            manager.wait_tasks(task_ids)
    finally:
        manager.shutdown()


def test_kill_is_idempotent(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    try:
        started = _start(manager, tmp_path, "import time; time.sleep(5)")

        killed = manager.kill(started["task_id"])
        killed_again = manager.kill(started["task_id"])
        snapshot = manager.get_output([started["task_id"]])[0]

        assert killed["outcome"] == "killed"
        assert killed_again["outcome"] == "already_exited"
        assert snapshot["status"] == "cancelled"
    finally:
        manager.shutdown()


def test_shutdown_terminates_all_running_commands(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    started = []
    try:
        started = [
            _start(manager, tmp_path, "import time; time.sleep(5)"),
            _start(manager, tmp_path, "import time; time.sleep(5)"),
        ]
        manager.shutdown()

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
            _pid_exists(task["pid"]) for task in started
        ):
            time.sleep(0.01)

        assert not any(_pid_exists(task["pid"]) for task in started)
    finally:
        manager.shutdown()


@pytest.mark.skipif(os.name == "nt", reason="POSIX process-group behavior")
def test_shutdown_kills_children_after_shell_leader_exits(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    started = manager.start_command(
        "sleep 30 &",
        tmp_path,
        shell_path="/bin/bash",
        background=True,
    )

    manager.shutdown()

    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(started["pid"], 0)
        except ProcessLookupError:
            break
        time.sleep(0.01)
    with pytest.raises(ProcessLookupError):
        os.killpg(started["pid"], 0)


def test_windows_job_termination_does_not_depend_on_shell_leader(monkeypatch):
    calls = []
    kernel32 = SimpleNamespace(
        TerminateJobObject=lambda handle, code: calls.append((handle, code))
    )
    task = SimpleNamespace(
        process=SimpleNamespace(poll=lambda: 0),
        windows_job=(kernel32, 42),
    )
    monkeypatch.setattr("navi_agent.runtime.task_manager.platform.system", lambda: "Windows")

    TaskManager()._terminate_process(task)

    assert calls == [(42, 1)]


def test_windows_job_handle_closes_once_when_task_finishes(monkeypatch):
    closed = []
    process = SimpleNamespace(pid=123, returncode=0, poll=lambda: 0, wait=lambda **_: 0)
    kernel32 = SimpleNamespace(CloseHandle=lambda handle: closed.append(handle))
    task = _Task(
        task_id="task",
        task_type="command",
        command="command",
        cwd=".",
        shell="powershell",
        process=process,
        started_at=time.time(),
        output_file="",
        timeout_seconds=None,
        background=True,
        tool_call_id=None,
        windows_job=(kernel32, 42),
    )
    reader = SimpleNamespace(is_alive=lambda: False, join=lambda: None)
    monkeypatch.setattr("navi_agent.runtime.task_manager.platform.system", lambda: "Windows")

    TaskManager()._watch_task(task, reader, None)

    assert task.status == "completed"
    assert task.done.is_set()
    assert task.windows_job is None
    assert closed == [42]


def test_worker_uses_shared_output_wait_and_kill(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    cancelled = threading.Event()

    def run_until_cancelled():
        cancelled.wait(timeout=2)
        raise KeyboardInterrupt("cancelled")

    try:
        completed = manager.start_worker(
            "a_complete",
            "inspect code",
            tmp_path,
            description="inspect",
            target=lambda: {
                "success": True,
                "content": "done",
                "subagent_type": "explore",
                "steps": 2,
                "tool_calls": 3,
            },
            cancel=lambda _reason: None,
            background=True,
        )
        running = manager.start_worker(
            "a_running",
            "run tests",
            tmp_path,
            description="tests",
            target=run_until_cancelled,
            cancel=lambda _reason: cancelled.set(),
            background=True,
        )

        snapshots = manager.wait_tasks(
            [completed["task_id"]],
            timeout_ms=2000,
        )
        killed = manager.kill(running["task_id"])
        stopped = manager.get_output([running["task_id"]], timeout_ms=2000)[0]

        assert snapshots[0]["status"] == "completed"
        assert snapshots[0]["output"] == "done"
        assert snapshots[0]["subagent_type"] == "explore"
        assert snapshots[0]["steps"] == 2
        assert snapshots[0]["tool_calls"] == 3
        assert killed["outcome"] == "killed"
        assert stopped["status"] == "cancelled"
    finally:
        manager.shutdown()


def test_foreground_command_can_move_to_background(tmp_path):
    manager = TaskManager(tmp_path / "logs")
    result: dict[str, dict] = {}

    def run_foreground() -> None:
        result["snapshot"] = _start(
            manager,
            tmp_path,
            "import time; print('started', flush=True); time.sleep(0.3); print('finished')",
            background=False,
        )

    worker = threading.Thread(target=run_foreground)
    worker.start()
    try:
        deadline = time.monotonic() + 2
        moved = None
        while moved is None and time.monotonic() < deadline:
            moved = manager.background_current()
            if moved is None:
                time.sleep(0.01)

        assert moved is not None
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert result["snapshot"]["task_id"] == moved["task_id"]
        assert result["snapshot"]["status"] == "running"

        finished = manager.get_output([moved["task_id"]], timeout_ms=3000)[0]
        assert finished["status"] == "completed"
        assert finished["output"] == "started\nfinished\n"
    finally:
        manager.shutdown()
        worker.join(timeout=1)


def test_natural_completion_emits_one_event(tmp_path):
    events: list[dict] = []
    completed = threading.Event()

    def on_event(event: dict) -> None:
        events.append(event)
        if event.get("type") == "task_completed":
            completed.set()

    manager = TaskManager(tmp_path / "logs", on_event=on_event)
    try:
        started = _start(manager, tmp_path, "print('complete')")

        assert completed.wait(timeout=2)
        manager.get_output([started["task_id"]], timeout_ms=2000)
        time.sleep(0.05)

        matching = [
            event
            for event in events
            if event.get("type") == "task_completed"
            and event.get("task", {}).get("task_id") == started["task_id"]
        ]
        assert len(matching) == 1
        assert matching[0]["task"]["status"] == "completed"
    finally:
        manager.shutdown()


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True
