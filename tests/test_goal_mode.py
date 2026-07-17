import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from navi_agent.runtime.goal import CONTINUE_PROMPT, GoalRunner, parse_goal_command
from navi_agent.storage.goal_store import GoalStore


class FakeRuntime:
    def __init__(self, responses, session_ids=None):
        self.responses = list(responses)
        self.session_ids = list(session_ids or [])
        self.prompts = []
        self.session_store = SimpleNamespace(session_id="session-1")
        self.goal_runner = None

    def run_turn(self, prompt, image_paths: list[Path] | None = None):
        self.prompts.append((prompt, image_paths))
        response = self.responses.pop(0)
        if self.session_ids:
            self.session_store.session_id = self.session_ids.pop(0)
        if isinstance(response, dict):
            return response
        if isinstance(response, tuple) and response[0] == "tokens":
            self.goal_runner.record_tokens(response[1])
        elif response in {"active", "complete", "paused", "blocked"}:
            self.goal_runner.update_goal(response)
        return {
            "ok": True,
            "final_answer": f"turn {response}",
            "content": f"turn {response}",
            "pending_attachments": [],
        }


def make_runner(tmp_path, responses, session_ids=None):
    runtime = FakeRuntime(responses, session_ids)
    runner = GoalRunner(runtime, GoalStore(tmp_path / "history.sqlite3"))
    runtime.goal_runner = runner
    return runtime, runner


def test_goal_store_persists_progress_budgets_and_rebinds(tmp_path):
    store = GoalStore(tmp_path / "history.sqlite3")
    goal = store.create("session-1", "finish the task", "tests pass")

    store.begin_turn(goal["goal_id"])
    store.record_tokens(goal["goal_id"], 123)
    store.set_budget(goal["goal_id"], 3, "turns")
    updated = store.rebind(goal["goal_id"], "session-2")

    assert updated["turns_used"] == 1
    assert updated["tokens_used"] == 123
    assert updated["turn_budget"] == 3
    assert store.current("session-1") is None
    assert store.current("session-2")["goal_id"] == goal["goal_id"]


def test_goal_store_migrates_the_pr_prototype_schema(tmp_path):
    db_path = tmp_path / "history.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE goals (
                goal_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                objective TEXT NOT NULL,
                status TEXT NOT NULL,
                cycle_count INTEGER NOT NULL DEFAULT 0,
                max_cycles INTEGER NOT NULL DEFAULT 20,
                last_summary TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO goals VALUES (
                'g_old', 'session-1', 'old objective', 'running', 3, 20,
                '', NULL, '2026-01-01', '2026-01-01'
            );
            """
        )

    migrated = GoalStore(db_path).current("session-1")

    assert migrated["status"] == "paused"
    assert migrated["turns_used"] == 3
    assert migrated["completion_criterion"] == ""


def test_normal_final_does_not_end_goal_and_continue_prompt_starts_next_turn(tmp_path):
    runtime, runner = make_runner(
        tmp_path,
        ["normal final", "complete"],
        session_ids=["session-2", "session-2"],
    )
    created = runner.create_goal("finish and verify the task", "focused checks pass")

    result = runner.drive("finish and verify the task")

    assert created["ok"] is True
    assert [prompt for prompt, _ in runtime.prompts] == [
        "finish and verify the task",
        CONTINUE_PROMPT,
    ]
    assert result["goal_status"] == "complete"
    assert result["goal"]["progress"]["turns"] == 2
    assert runner.current() is None


def test_blocked_goal_can_be_resumed(tmp_path):
    runtime, runner = make_runner(tmp_path, ["blocked", "complete"])
    runner.create_goal("wait for required input")

    blocked = runner.drive("wait for required input")
    resumed = runner.apply_command("resume", "")
    completed = runner.drive(resumed["run_input"])

    assert blocked["goal_status"] == "blocked"
    assert resumed["run_input"] == CONTINUE_PROMPT
    assert completed["goal_status"] == "complete"


def test_turn_and_token_budgets_block_before_another_turn(tmp_path):
    runtime, runner = make_runner(tmp_path / "turns", ["normal final"])
    runner.create_goal("one turn only")
    runner.set_goal_budget(1, "turns")

    turn_result = runner.drive("one turn only")

    assert len(runtime.prompts) == 1
    assert turn_result["goal_status"] == "blocked"
    assert "turn budget exhausted" in turn_result["goal"]["reason"]

    runtime, runner = make_runner(tmp_path / "tokens", [("tokens", 10)])
    runner.create_goal("ten tokens only")
    runner.set_goal_budget(10, "tokens")

    token_result = runner.drive("ten tokens only")

    assert len(runtime.prompts) == 1
    assert token_result["goal_status"] == "blocked"
    assert "token budget exhausted" in token_result["goal"]["reason"]


def test_token_budget_enforcement_blocks_at_safe_model_tool_step_boundary(tmp_path):
    _, runner = make_runner(tmp_path, [])
    runner.create_goal("bounded tool work")
    runner.set_goal_budget(10, "tokens")
    runner.record_tokens(10)

    reason = runner.enforce_token_budget_after_step()

    assert "token budget exhausted" in reason
    assert runner.current()["status"] == "blocked"


def test_turn_error_and_resume_normalization_pause_goal(tmp_path):
    runtime, runner = make_runner(
        tmp_path,
        [{"ok": False, "error": "network error", "final_answer": "", "content": ""}],
    )
    runner.create_goal("handle an error")

    result = runner.drive("handle an error")
    normalized = runner.store.normalize_interrupted(runtime.session_store.session_id)

    assert result["goal_status"] == "paused"
    assert normalized["status"] == "paused"
    assert normalized["status_reason"] == "network error"

    runner.store.set_status(normalized["goal_id"], "active")
    normalized = runner.store.normalize_interrupted(runtime.session_store.session_id)
    assert normalized["status"] == "paused"
    assert normalized["status_reason"] == "paused after session resume"


def test_goal_reminder_is_dynamic_escaped_and_budget_aware(tmp_path):
    _, runner = make_runner(tmp_path, [])
    runner.create_goal("use <input> safely", "all tests pass")
    runner.set_goal_budget(4, "turns")
    goal = runner.current()
    runner.store.begin_turn(goal["goal_id"])
    runner.store.begin_turn(goal["goal_id"])
    runner.store.begin_turn(goal["goal_id"])

    reminder = runner.build_reminder()

    assert "<untrusted_objective>use &lt;input&gt; safely</untrusted_objective>" in reminder
    assert "<completion_criterion>all tests pass</completion_criterion>" in reminder
    assert "nearing its budget" in reminder
    assert "normal final answer does not complete" in reminder

    runner.update_goal("paused")
    paused = runner.build_reminder()
    assert "Do not pursue this goal autonomously" in paused


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/goal", ("status", "")),
        ("/goal status", ("status", "")),
        ("/goal pause", ("pause", "")),
        ("/goal resume", ("resume", "")),
        ("/goal cancel", ("cancel", "")),
        ("/goal replace ship it", ("replace", "ship it")),
        ("/goal ship it", ("create", "ship it")),
        ("/goal status extra", ("usage", "")),
        ("/goal replace", ("usage", "")),
        ("/goals", None),
    ],
)
def test_parse_goal_command(text, expected):
    assert parse_goal_command(text) == expected


def test_budget_units_validate_and_merge(tmp_path):
    _, runner = make_runner(tmp_path, [])
    runner.create_goal("budgeted task")

    assert runner.set_goal_budget(2.4, "turns")["goal"]["budgets"]["turns"] == 2
    assert runner.set_goal_budget(5000, "tokens")["goal"]["budgets"]["tokens"] == 5000
    assert runner.set_goal_budget(2, "minutes")["goal"]["budgets"]["milliseconds"] == 120000
    assert runner.set_goal_budget(999, "milliseconds")["ok"] is False
    assert runner.set_goal_budget(25, "hours")["ok"] is False
    assert runner.set_goal_budget(True, "turns")["ok"] is False
