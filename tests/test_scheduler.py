from __future__ import annotations

import threading

import pytest

from navi_agent.runtime.scheduler import Scheduler
from navi_agent.storage.scheduler_store import SchedulerStore


def test_create_defaults_clamps_interval_and_enforces_limit(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = Scheduler("session", store, lambda event: None, max_tasks=1, now=lambda: 1000)

    task = scheduler.create("1s", "check deploy")

    assert len(task["id"]) == 12
    assert task["human_schedule"] == "every 1 minute"
    assert task["recurring"] is True
    assert set(task) == {"id", "human_schedule", "recurring"}
    assert scheduler.run_due(1059) == []
    assert len(scheduler.run_due(1060)) == 1
    assert set(scheduler.list()["tasks"][0]) == {
        "id",
        "prompt",
        "interval_human",
        "next_fire_at",
        "created_at",
        "recurring",
    }
    with pytest.raises(ValueError, match="maximum of 1"):
        scheduler.create("5m", "too many")


@pytest.mark.parametrize("interval", ["", "5", "m", "5x", "0m"])
def test_create_rejects_invalid_interval(tmp_path, interval):
    scheduler = Scheduler(
        "session",
        SchedulerStore(tmp_path / "scheduler.db"),
        lambda event: None,
        now=lambda: 1000,
    )

    with pytest.raises(ValueError):
        scheduler.create(interval, "prompt")


def test_only_durable_tasks_are_persisted_and_sessions_are_isolated(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = Scheduler("one", store, lambda event: None, now=lambda: 1000)
    scheduler.create("5m", "ephemeral")
    durable = scheduler.create("5m", "durable", durable=True)
    other = Scheduler("two", store, lambda event: None, now=lambda: 1000)
    other.create("5m", "other", durable=True)

    assert [task["id"] for task in store.load("one")] == [durable["id"]]
    assert [task["prompt"] for task in store.load("two")] == ["other"]


def test_missed_durable_one_shot_fires_once_and_is_deleted(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = [1000.0]
    first = Scheduler("session", SchedulerStore(path), lambda event: None, now=lambda: clock[0])
    task = first.create("1m", "run once", recurring=False, durable=True)
    first.close()

    clock[0] = 1300
    events = []
    resumed_store = SchedulerStore(path)
    resumed = Scheduler("session", resumed_store, events.append, now=lambda: clock[0])

    assert resumed.run_due() == [
        {
            "type": "scheduled_prompt",
            "task_id": task["id"],
            "prompt": "run once",
            "human_schedule": "every 1 minute",
        }
    ]
    assert resumed.run_due() == []
    assert events == [
        {
            "type": "scheduled_prompt",
            "task_id": task["id"],
            "prompt": "run once",
            "human_schedule": "every 1 minute",
        }
    ]
    assert resumed.list() == {"tasks": []}
    assert resumed_store.load("session") == []


def test_overdue_recurring_coalesces_and_waits_for_completion(tmp_path):
    path = tmp_path / "scheduler.db"
    clock = [1000.0]
    first = Scheduler("session", SchedulerStore(path), lambda event: None, now=lambda: clock[0])
    task = first.create("1m", "repeat", durable=True)
    first.close()

    clock[0] = 1300
    events = []
    resumed_store = SchedulerStore(path)
    resumed = Scheduler(
        "session",
        resumed_store,
        events.append,
        now=lambda: clock[0],
    )

    assert len(resumed.run_due()) == 1
    assert resumed_store.load("session")[0]["next_fire_at"] == 1360
    clock[0] = 2000
    assert resumed.run_due() == []
    assert resumed.complete(task["id"]) is True
    assert len(resumed.run_due()) == 1
    assert resumed_store.load("session")[0]["next_fire_at"] == 2060
    assert len(events) == 2


def test_delete_removes_memory_and_durable_state(tmp_path):
    store = SchedulerStore(tmp_path / "scheduler.db")
    scheduler = Scheduler("session", store, lambda event: None, now=lambda: 1000)
    task = scheduler.create("5m", "delete me", durable=True)

    assert scheduler.delete(task["id"])["success"] is True
    assert scheduler.delete(task["id"])["success"] is False
    assert scheduler.list() == {"tasks": []}
    assert store.load("session") == []


def test_condition_worker_fires_immediate_task_without_waiting_for_interval(tmp_path):
    fired = threading.Event()
    events = []

    def on_fire(event):
        events.append(event)
        fired.set()

    scheduler = Scheduler(
        "session",
        SchedulerStore(tmp_path / "scheduler.db"),
        on_fire,
        now=lambda: 1000,
    )
    scheduler.create("1h", "now", recurring=False, fire_immediately=True)
    scheduler.start()
    try:
        assert fired.wait(1)
        assert [event["prompt"] for event in events] == ["now"]
    finally:
        scheduler.close()
