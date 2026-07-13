from types import SimpleNamespace

from navi_agent.runtime.goal import GoalRunner
from navi_agent.storage.goal_store import GoalStore
from navi_agent.tools.registry import ToolRegistry


class FakeRuntime:
    def __init__(self, responses, session_ids=None):
        self.responses = list(responses)
        self.session_ids = list(session_ids or [])
        self.prompts = []
        self.session_store = SimpleNamespace(session_id="session-1")
        self.tool_registry = ToolRegistry()
        self._tools_for_api = [{"type": "function", "function": {"name": "read_file"}}]
        self.approval_manager = SimpleNamespace(READ_ONLY_TOOLS={"read_file"})

    def run_turn(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if self.session_ids:
            self.session_store.session_id = self.session_ids.pop(0)
        if isinstance(response, dict):
            return response
        self.tool_registry.invoke(
            "goal_status",
            {
                "status": response,
                "summary": f"reported {response}",
            },
        )
        return {
            "ok": True,
            "final_answer": f"turn {response}",
            "content": f"turn {response}",
        }


def test_goal_store_persists_latest_goal(tmp_path):
    store = GoalStore(tmp_path / "history.sqlite3")

    goal = store.create("session-1", "finish the task", max_cycles=5)
    updated = store.update(goal["goal_id"], status="running", cycle_count=2)

    assert updated["status"] == "running"
    assert updated["cycle_count"] == 2
    assert store.latest("session-1")["goal_id"] == goal["goal_id"]
    assert store.latest("session-1", active_only=True)["goal_id"] == goal["goal_id"]


def test_goal_runner_requires_verification_before_completion(tmp_path):
    runtime = FakeRuntime(
        ["ready", "completed"],
        session_ids=["session-2", "session-2"],
    )
    store = GoalStore(tmp_path / "history.sqlite3")
    runner = GoalRunner(runtime, store)
    goal = runner.create("finish and verify the task")

    result = runner.run(goal["goal_id"])

    assert result["goal_status"] == "completed"
    assert result["goal"]["cycle_count"] == 2
    assert store.latest("session-2")["goal_id"] == goal["goal_id"]
    assert "Independently verify" in runtime.prompts[1]
    assert not runtime.tool_registry.has("goal_status")
    assert runtime._tools_for_api == [{"type": "function", "function": {"name": "read_file"}}]
    assert runtime.approval_manager.READ_ONLY_TOOLS == {"read_file"}


def test_goal_runner_blocks_and_can_resume(tmp_path):
    store = GoalStore(tmp_path / "history.sqlite3")
    first_runtime = FakeRuntime(["blocked"])
    goal = GoalRunner(first_runtime, store).create("wait for required input")

    blocked = GoalRunner(first_runtime, store).run(goal["goal_id"])
    assert blocked["goal_status"] == "blocked"

    second_runtime = FakeRuntime(["ready", "completed"])
    completed = GoalRunner(second_runtime, store).run(
        goal["goal_id"],
        note="use the supplied value",
    )

    assert completed["goal_status"] == "completed"
    assert "Additional user guidance: use the supplied value" in second_runtime.prompts[0]


def test_goal_runner_pauses_on_turn_error_and_cycle_limit(tmp_path):
    store = GoalStore(tmp_path / "history.sqlite3")
    error_runtime = FakeRuntime([
        {"ok": False, "error": "network error", "final_answer": "", "content": ""}
    ])
    error_goal = GoalRunner(error_runtime, store).create("handle an error")

    errored = GoalRunner(error_runtime, store).run(error_goal["goal_id"])
    assert errored["goal_status"] == "paused"
    assert "network error" in errored["error"]

    limit_runtime = FakeRuntime(["continue"])
    limit_runner = GoalRunner(limit_runtime, store)
    limit_goal = limit_runner.create("keep working", max_cycles=1)

    limited = limit_runner.run(limit_goal["goal_id"])
    assert limited["goal_status"] == "paused"
    assert limited["goal"]["cycle_count"] == 1
    assert "maximum number of cycles" in limited["error"]
