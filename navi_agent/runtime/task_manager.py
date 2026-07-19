from __future__ import annotations

import os
import platform
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timed_out"}


@dataclass
class _Task:
    task_id: str
    task_type: str
    command: str
    cwd: str
    shell: str
    process: subprocess.Popen | None
    started_at: float
    output_file: str
    timeout_seconds: float | None
    background: bool
    tool_call_id: str | None
    description: str = ""
    status: str = "running"
    exit_code: int | None = None
    ended_at: float | None = None
    output: str = ""
    truncated: bool = False
    cancelled: bool = False
    timed_out: bool = False
    suppress_completion: bool = False
    waiters: int = 0
    cancel_callback: Callable[[str], None] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    windows_job: tuple[Any, Any] | None = None
    done: threading.Event = field(default_factory=threading.Event)
    detached: threading.Event = field(default_factory=threading.Event)


class TaskManager:
    """Own background subprocesses and their session-scoped output."""

    def __init__(
        self,
        log_dir: str | Path | None = None,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        max_background_tasks: int = 10,
        max_output_chars: int = 50_000,
    ) -> None:
        self.log_dir = Path(log_dir) if log_dir is not None else None
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        self.on_event = on_event
        self.max_background_tasks = max_background_tasks
        self.max_output_chars = max_output_chars
        self._tasks: dict[str, _Task] = {}
        self._condition = threading.Condition()
        self._closing = False

    def start_command(
        self,
        command: str,
        cwd: str | Path,
        *,
        shell_path: str,
        shell: str = "bash",
        background: bool = False,
        timeout_seconds: float | None = None,
        encoding: str = "utf-8",
        tool_call_id: str | None = None,
        description: str = "",
        task_type: str = "command",
        on_output: Callable[[str], None] | None = None,
        on_line: Callable[[str, str, str], None] | None = None,
        on_done: Callable[[str], None] | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:12]
        target_cwd = str(Path(cwd).resolve())
        output_file = ""
        if self.log_dir is not None:
            output_file = str(self.log_dir / f"{task_type}-{task_id}.log")

        if shell == "powershell":
            argv = [
                shell_path,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8; " + command,
            ]
        else:
            argv = [shell_path, "-lc", command]
        popen_kwargs: dict[str, Any] = {
            "cwd": target_cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
        }
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_BREAKAWAY_FROM_JOB
                | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            )
        else:
            popen_kwargs["start_new_session"] = True

        with self._condition:
            if self._closing:
                raise RuntimeError("task manager is shutting down")
            if background:
                active = sum(
                    task.status == "running"
                    and (task.background or task.task_type == "subagent")
                    for task in self._tasks.values()
                )
                if active >= self.max_background_tasks:
                    raise RuntimeError(
                        f"maximum of {self.max_background_tasks} background tasks reached"
                    )
            try:
                process = subprocess.Popen(argv, **popen_kwargs)
            except OSError as exc:
                if platform.system() != "Windows" or exc.winerror != 5:
                    raise
                popen_kwargs["creationflags"] &= ~subprocess.CREATE_BREAKAWAY_FROM_JOB
                process = subprocess.Popen(argv, **popen_kwargs)
            windows_job = None
            if platform.system() == "Windows":
                try:
                    windows_job = self._create_windows_job(process)
                except Exception:
                    process.kill()
                    process.wait()
                    raise
            task = _Task(
                task_id=task_id,
                task_type=task_type,
                command=command,
                cwd=target_cwd,
                shell=shell,
                process=process,
                started_at=time.time(),
                output_file=output_file,
                timeout_seconds=(
                    timeout_seconds if timeout_seconds and timeout_seconds > 0 else None
                ),
                background=background,
                tool_call_id=tool_call_id,
                description=description,
                windows_job=windows_job,
            )
            self._tasks[task_id] = task

        reader = threading.Thread(
            target=self._read_output,
            args=(task, encoding, on_output, on_line),
            name=f"navi-task-output-{task_id}",
            daemon=True,
        )
        watcher = threading.Thread(
            target=self._watch_task,
            args=(task, reader, on_done),
            name=f"navi-task-{task_id}",
            daemon=True,
        )
        reader.start()
        watcher.start()

        if background:
            with self._condition:
                return self._snapshot_locked(task)

        while not task.done.wait(0.05):
            if task.detached.is_set():
                with self._condition:
                    return self._snapshot_locked(task)
            if is_cancelled is not None and is_cancelled():
                self.kill(task_id)
                with self._condition:
                    result = self._snapshot_locked(task)
                    if not task.background:
                        self._tasks.pop(task_id, None)
                result["interrupted"] = True
                return result
        with self._condition:
            result = self._snapshot_locked(task)
            if not task.background:
                self._tasks.pop(task_id, None)
            return result

    def background_current(
        self,
        tool_call_id: str | None = None,
        task_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._condition:
            candidates = [
                task
                for task in self._tasks.values()
                if task.status == "running"
                and not task.background
                and (tool_call_id is None or task.tool_call_id == tool_call_id)
                and (task_id is None or task.task_id == task_id)
            ]
            if not candidates:
                return None
            task = max(candidates, key=lambda item: item.started_at)
            active = sum(
                item.status == "running"
                and (item.background or item.task_type == "subagent")
                for item in self._tasks.values()
                if item is not task
            )
            if active >= self.max_background_tasks:
                return None
            task.background = True
            task.detached.set()
            snapshot = self._snapshot_locked(task)
            self._condition.notify_all()
        if self.on_event is not None:
            self.on_event({"type": "task_backgrounded", "task": snapshot})
        return snapshot

    def start_worker(
        self,
        task_id: str,
        command: str,
        cwd: str | Path,
        *,
        description: str,
        target: Callable[[], dict[str, Any]],
        cancel: Callable[[str], None],
        background: bool,
        tool_call_id: str | None = None,
    ) -> dict[str, Any]:
        output_file = (
            str(self.log_dir / f"subagent-{task_id}.log")
            if self.log_dir is not None
            else ""
        )
        with self._condition:
            if self._closing:
                raise RuntimeError("task manager is shutting down")
            active = sum(
                task.status == "running"
                and (task.background or task.task_type == "subagent")
                for task in self._tasks.values()
            )
            if active >= self.max_background_tasks:
                raise RuntimeError(
                    f"maximum of {self.max_background_tasks} background tasks reached"
                )
            task = _Task(
                task_id=task_id,
                task_type="subagent",
                command=command,
                cwd=str(Path(cwd).resolve()),
                shell="",
                process=None,
                started_at=time.time(),
                output_file=output_file,
                timeout_seconds=None,
                background=background,
                tool_call_id=tool_call_id,
                description=description,
                cancel_callback=cancel,
            )
            self._tasks[task_id] = task

        threading.Thread(
            target=self._run_worker,
            args=(task, target),
            name=f"navi-subagent-{task_id}",
            daemon=True,
        ).start()
        with self._condition:
            return self._snapshot_locked(task)

    def get_output(
        self,
        task_ids: list[str],
        timeout_ms: int | None = 0,
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(task_id.strip() for task_id in task_ids if task_id.strip()))
        if not ids:
            raise ValueError("task_ids must not be empty")
        if len(ids) > 20:
            raise ValueError("task_ids exceeds maximum of 20 entries")
        timeout = min(max(0, timeout_ms or 0), 600_000) / 1000
        if timeout:
            self._wait(ids, "wait_all", timeout)
        with self._condition:
            return [
                self._snapshot_locked(self._tasks[task_id])
                if task_id in self._tasks
                else {"task_id": task_id, "status": "not_found"}
                for task_id in ids
            ]

    def wait_tasks(
        self,
        task_ids: list[str],
        *,
        mode: str = "wait_all",
        timeout_ms: int | None = 30_000,
    ) -> list[dict[str, Any]]:
        ids = list(dict.fromkeys(task_id.strip() for task_id in task_ids if task_id.strip()))
        if not ids:
            raise ValueError("task_ids must not be empty")
        if len(ids) > 20:
            raise ValueError("task_ids exceeds maximum of 20 entries")
        if mode not in {"wait_any", "wait_all"}:
            raise ValueError("mode must be wait_any or wait_all")
        timeout = min(
            max(0, 30_000 if timeout_ms is None else timeout_ms),
            600_000,
        ) / 1000
        if timeout:
            self._wait(ids, mode, timeout)
        with self._condition:
            return [
                self._snapshot_locked(self._tasks[task_id])
                if task_id in self._tasks
                else {"task_id": task_id, "status": "not_found"}
                for task_id in ids
            ]

    def kill(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                return {
                    "task_id": task_id,
                    "outcome": "not_found",
                    "message": f"Task {task_id} not found.",
                }
            if task.status in TERMINAL_STATUSES:
                return {
                    "task_id": task_id,
                    "outcome": "already_exited",
                    "message": f"Task {task_id} has already exited.",
                }
            task.cancelled = True
            task.suppress_completion = True
            cancel_callback = task.cancel_callback
        if cancel_callback is not None:
            cancel_callback(f"Task {task_id} was killed")
        else:
            self._terminate_process(task)
        task.done.wait(2)
        return {
            "task_id": task_id,
            "outcome": "killed",
            "message": f"Task {task_id} was killed.",
        }

    def shutdown(self) -> None:
        with self._condition:
            self._closing = True
            tasks = list(self._tasks.values())
            running_ids = [task.task_id for task in tasks if task.status == "running"]
        for task_id in running_ids:
            self.kill(task_id)
        for task in tasks:
            task.done.wait(2)

    def _wait(self, task_ids: list[str], mode: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            waiting = [
                self._tasks[task_id]
                for task_id in task_ids
                if task_id in self._tasks and self._tasks[task_id].status == "running"
            ]
            detach_waiting = [task for task in waiting if not task.background]
            for task in waiting:
                task.waiters += 1
            try:
                while True:
                    if any(task.detached.is_set() for task in detach_waiting):
                        return
                    states = [
                        self._tasks[task_id].status
                        if task_id in self._tasks
                        else "not_found"
                        for task_id in task_ids
                    ]
                    if mode == "wait_all" and all(
                        state in TERMINAL_STATUSES or state == "not_found"
                        for state in states
                    ):
                        return
                    if mode == "wait_any" and (
                        any(state in TERMINAL_STATUSES for state in states)
                        or all(state == "not_found" for state in states)
                    ):
                        return
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return
                    self._condition.wait(remaining)
            finally:
                for task in waiting:
                    task.waiters -= 1

    def _snapshot_locked(self, task: _Task) -> dict[str, Any]:
        ended_at = task.ended_at
        duration = (ended_at or time.time()) - task.started_at
        return {
            "task_id": task.task_id,
            "task_type": task.task_type,
            "command": task.command,
            "description": task.description,
            "cwd": task.cwd,
            "shell": task.shell,
            "status": task.status,
            "pid": task.process.pid if task.process is not None else None,
            "exit_code": task.exit_code,
            "started": datetime.fromtimestamp(task.started_at, timezone.utc).isoformat(),
            "ended": (
                datetime.fromtimestamp(ended_at, timezone.utc).isoformat()
                if ended_at is not None
                else None
            ),
            "duration_secs": max(0.0, duration),
            "output": task.output,
            "output_file": task.output_file,
            "truncated": task.truncated,
            **task.metadata,
        }

    def _read_output(
        self,
        task: _Task,
        encoding: str,
        on_output: Callable[[str], None] | None,
        on_line: Callable[[str, str, str], None] | None,
    ) -> None:
        assert task.process is not None
        log = (
            Path(task.output_file).open("w", encoding="utf-8", newline="")
            if task.output_file
            else None
        )
        logged_chars = 0
        try:
            assert task.process.stdout is not None
            for raw_line in task.process.stdout:
                text = raw_line.decode(encoding, errors="replace")
                if log is not None and logged_chars < self.max_output_chars:
                    chunk = text[: self.max_output_chars - logged_chars]
                    log.write(chunk)
                    log.flush()
                    logged_chars += len(chunk)
                with self._condition:
                    task.output += text
                    if len(task.output) > self.max_output_chars:
                        task.output = task.output[-self.max_output_chars :]
                        task.truncated = True
                    foreground = not task.background
                if foreground and on_output is not None:
                    on_output(text)
                if on_line is not None and text.strip():
                    on_line(task.task_id, task.description, text.rstrip("\r\n"))
        finally:
            if task.process.stdout is not None:
                task.process.stdout.close()
            if log is not None:
                log.close()

    def _watch_task(
        self,
        task: _Task,
        reader: threading.Thread,
        on_done: Callable[[str], None] | None,
    ) -> None:
        assert task.process is not None
        deadline = (
            time.monotonic() + task.timeout_seconds
            if task.timeout_seconds is not None
            else None
        )
        while True:
            if platform.system() == "Windows":
                running = task.process.poll() is None or reader.is_alive()
            else:
                task.process.poll()
                try:
                    os.killpg(task.process.pid, 0)
                    running = True
                except ProcessLookupError:
                    running = False
            if not running:
                break
            if deadline is not None and time.monotonic() >= deadline:
                with self._condition:
                    task.timed_out = True
                self._terminate_process(task)
                break
            time.sleep(0.05)
        try:
            task.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._kill_process(task)
            task.process.wait()
        reader.join()
        if on_done is not None:
            on_done(task.task_id)

        with self._condition:
            task.exit_code = task.process.returncode
            task.ended_at = time.time()
            if task.cancelled:
                task.status = "cancelled"
            elif task.timed_out:
                task.status = "timed_out"
            elif task.exit_code == 0:
                task.status = "completed"
            else:
                task.status = "failed"
            should_emit = (
                task.background
                and not task.suppress_completion
                and task.waiters == 0
                and not self._closing
            )
            if task.windows_job is not None:
                kernel32, job = task.windows_job
                task.windows_job = None
                kernel32.CloseHandle(job)
            snapshot = self._snapshot_locked(task)
            task.done.set()
            self._condition.notify_all()
        if should_emit:
            if self.on_event is not None:
                self.on_event({"type": "task_completed", "task": snapshot})

    def _run_worker(
        self,
        task: _Task,
        target: Callable[[], dict[str, Any]],
    ) -> None:
        cancelled = False
        try:
            result = target()
            success = result.get("success", True)
            output = str(result.get("content") or result.get("error") or "")
            metadata = {
                key: value
                for key, value in result.items()
                if key not in {"success", "content", "error"}
            }
            if result.get("error"):
                metadata["error"] = str(result["error"])
        except KeyboardInterrupt as exc:
            cancelled = True
            success = False
            output = str(exc)
            metadata = {}
        except Exception as exc:
            success = False
            output = str(exc)
            metadata = {"error": str(exc)}

        if task.output_file:
            Path(task.output_file).write_text(
                output[-self.max_output_chars :], encoding="utf-8"
            )
        with self._condition:
            task.output = output[-self.max_output_chars :]
            task.truncated = len(output) > self.max_output_chars
            task.metadata.update(metadata)
            task.ended_at = time.time()
            if task.cancelled or cancelled:
                task.status = "cancelled"
                task.exit_code = None
            elif success:
                task.status = "completed"
                task.exit_code = 0
            else:
                task.status = "failed"
                task.exit_code = 1
            should_emit = (
                task.background
                and not task.suppress_completion
                and task.waiters == 0
                and not self._closing
            )
            snapshot = self._snapshot_locked(task)
            task.done.set()
            self._condition.notify_all()
        if should_emit and self.on_event is not None:
            self.on_event({"type": "task_completed", "task": snapshot})

    def _terminate_process(self, task: _Task) -> None:
        process = task.process
        assert process is not None
        if platform.system() == "Windows":
            with self._condition:
                if task.windows_job is not None:
                    kernel32, job = task.windows_job
                    kernel32.TerminateJobObject(job, 1)
                    return
            if process.poll() is not None:
                return
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
            return
        process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            process.poll()
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        self._kill_process(task, process_group)

    @staticmethod
    def _kill_process(
        task: _Task,
        process_group: int | None = None,
    ) -> None:
        process = task.process
        assert process is not None
        if platform.system() == "Windows":
            if task.windows_job is not None:
                kernel32, job = task.windows_job
                kernel32.TerminateJobObject(job, 1)
                return
            if process.poll() is None:
                process.kill()
            return
        if process_group is None:
            process_group = process.pid
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            pass

    @staticmethod
    def _create_windows_job(process: subprocess.Popen) -> tuple[Any, Any] | None:
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_int64),
                ("per_job_user_time_limit", ctypes.c_int64),
                ("limit_flags", ctypes.c_uint32),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", ctypes.c_uint32),
                ("affinity", ctypes.c_size_t),
                ("priority_class", ctypes.c_uint32),
                ("scheduling_class", ctypes.c_uint32),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operation_count", ctypes.c_uint64),
                ("write_operation_count", ctypes.c_uint64),
                ("other_operation_count", ctypes.c_uint64),
                ("read_transfer_count", ctypes.c_uint64),
                ("write_transfer_count", ctypes.c_uint64),
                ("other_transfer_count", ctypes.c_uint64),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", BasicLimitInformation),
                ("io_info", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = ctypes.c_void_p
        kernel32.SetInformationJobObject.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        info = ExtendedLimitInformation()
        info.basic_limit_information.limit_flags = 0x2000
        if not kernel32.SetInformationJobObject(
            job,
            9,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(job)
            raise ctypes.WinError(error)
        if not kernel32.AssignProcessToJobObject(job, ctypes.c_void_p(process._handle)):
            kernel32.CloseHandle(job)
            return None
        return kernel32, job
