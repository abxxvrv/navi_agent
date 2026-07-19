from __future__ import annotations

import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from ..storage.scheduler_store import SchedulerStore


_EXPIRY_SECONDS = 7 * 24 * 60 * 60
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _human_schedule(seconds: int) -> str:
    for unit_seconds, singular, plural in (
        (86400, "day", "days"),
        (3600, "hour", "hours"),
        (60, "minute", "minutes"),
    ):
        if seconds % unit_seconds == 0:
            count = seconds // unit_seconds
            return f"every {count} {singular if count == 1 else plural}"
    return f"every {seconds} seconds"


class Scheduler:
    def __init__(
        self,
        session_id: str,
        store: SchedulerStore,
        on_fire: Callable[[dict[str, Any]], None],
        max_tasks: int = 50,
        now: Callable[[], float] = time.time,
    ):
        self.session_id = session_id
        self.store = store
        self.on_fire = on_fire
        self.max_tasks = max_tasks
        self._now = now
        self._condition = threading.Condition()
        self._tasks = {
            task["id"]: {**task, "in_flight": False}
            for task in self.store.load(session_id)
        }
        self._closed = False
        self._thread: threading.Thread | None = None

    def create(
        self,
        interval: str,
        prompt: str,
        recurring: bool = True,
        durable: bool = False,
        fire_immediately: bool = False,
    ) -> dict[str, Any]:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        value = interval.strip()
        if len(value) < 2 or value[-1] not in _UNIT_SECONDS or not value[:-1].isdigit():
            raise ValueError(
                f"invalid interval format: {interval!r} (expected e.g. 5m, 2h, 1d)"
            )
        amount = int(value[:-1])
        if amount == 0:
            raise ValueError("interval value must be greater than 0")
        interval_seconds = max(60, amount * _UNIT_SECONDS[value[-1]])

        with self._condition:
            if len(self._tasks) >= self.max_tasks:
                raise ValueError(f"maximum of {self.max_tasks} scheduled tasks reached")
            current = self._now()
            task = {
                "id": uuid.uuid4().hex[:12],
                "interval_seconds": interval_seconds,
                "prompt": prompt,
                "recurring": recurring,
                "durable": durable,
                "created_at": current,
                "next_fire_at": current if fire_immediately else current + interval_seconds,
                "expires_at": current + _EXPIRY_SECONDS if recurring else None,
                "in_flight": False,
            }
            if durable:
                self.store.upsert(self.session_id, task)
            self._tasks[task["id"]] = task
            self._condition.notify_all()
            return {
                "id": task["id"],
                "human_schedule": _human_schedule(interval_seconds),
                "recurring": recurring,
            }

    def list(self) -> dict[str, list[dict[str, Any]]]:
        with self._condition:
            tasks = []
            for task in self._tasks.values():
                prompt = task["prompt"]
                tasks.append({
                    "id": task["id"],
                    "prompt": prompt[:80] + ("..." if len(prompt) > 80 else ""),
                    "interval_human": _human_schedule(task["interval_seconds"]),
                    "next_fire_at": datetime.fromtimestamp(
                        task["next_fire_at"], timezone.utc
                    ).isoformat(),
                    "created_at": datetime.fromtimestamp(
                        task["created_at"], timezone.utc
                    ).isoformat(),
                    "recurring": task["recurring"],
                })
            return {"tasks": tasks}

    def delete(self, id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.pop(id, None)
            if task is None:
                return {
                    "success": False,
                    "message": (
                        f"No scheduled task with ID {id} found. "
                        "Use scheduler_list to see active tasks."
                    ),
                }
            if task["durable"]:
                self.store.delete(self.session_id, id)
            self._condition.notify_all()
            return {
                "success": True,
                "message": f"Scheduled task {id} cancelled.",
            }

    def start(self) -> None:
        with self._condition:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._run,
                name=f"navi-scheduler-{self.session_id}",
                daemon=True,
            )
            self._thread.start()

    def run_due(self, now: float | None = None) -> list[dict[str, Any]]:
        current = self._now() if now is None else now
        events = []
        with self._condition:
            if self._closed:
                return events
            for task_id, task in list(self._tasks.items()):
                if task["in_flight"] or task["next_fire_at"] > current:
                    continue
                task["in_flight"] = True
                event = {
                    "type": "scheduled_prompt",
                    "task_id": task_id,
                    "prompt": task["prompt"],
                    "human_schedule": _human_schedule(task["interval_seconds"]),
                }
                events.append(event)

                if not task["recurring"] or (
                    task["expires_at"] is not None and current >= task["expires_at"]
                ):
                    del self._tasks[task_id]
                    if task["durable"]:
                        self.store.delete(self.session_id, task_id)
                else:
                    task["next_fire_at"] = current + task["interval_seconds"]
                    if task["durable"]:
                        self.store.upsert(self.session_id, task)
            self._condition.notify_all()

        for event in events:
            self.on_fire(event)
        return events

    def complete(self, task_id: str) -> bool:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or not task["in_flight"]:
                return False
            task["in_flight"] = False
            self._condition.notify_all()
            return True

    def rebind(self, session_id: str) -> None:
        with self._condition:
            durable_tasks = [
                task for task in self._tasks.values() if task["durable"]
            ]
            for task in durable_tasks:
                self.store.upsert(session_id, task)
            for task in durable_tasks:
                self.store.delete(self.session_id, task["id"])
            self.session_id = session_id

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while True:
            self.run_due()
            with self._condition:
                if self._closed:
                    return
                current = self._now()
                deadlines = [
                    task["next_fire_at"]
                    for task in self._tasks.values()
                    if not task["in_flight"]
                ]
                if deadlines:
                    self._condition.wait(max(0, min(deadlines) - current))
                else:
                    self._condition.wait()
