from __future__ import annotations

from typing import Any

from ..storage.goal_store import GoalStore


class GoalRunner:
    """Run repeated agent turns until a persisted goal is verified or paused."""

    def __init__(self, runtime: Any, store: GoalStore):
        self.runtime = runtime
        self.store = store

    def create(self, objective: str, max_cycles: int = 20) -> dict[str, Any]:
        objective = objective.strip()
        if not objective:
            raise ValueError("Goal objective cannot be empty.")
        return self.store.create(
            self.runtime.session_store.session_id,
            objective,
            max_cycles=max_cycles,
        )

    def run(self, goal_id: str, *, note: str = "") -> dict[str, Any]:
        goal = self.store.get(goal_id)
        if goal["status"] in {"completed", "cancelled"}:
            return {
                "ok": False,
                "error": f"Goal is already {goal['status']}.",
                "final_answer": "",
                "content": "",
                "goal_status": goal["status"],
                "goal": goal,
            }

        signal: dict[str, str] = {}

        def record_status(status: str, summary: str, blocker: str = "") -> dict[str, Any]:
            signal.clear()
            signal.update(status=status, summary=summary.strip(), blocker=blocker.strip())
            return {"ok": True, "message": "Goal status recorded. Finish this turn."}

        previous_tools = self.runtime._tools_for_api
        previous_read_only = self.runtime.approval_manager.READ_ONLY_TOOLS
        if self.runtime.tool_registry.has("goal_status"):
            raise RuntimeError("goal_status tool is already registered")

        self.runtime.tool_registry.register(
            name="goal_status",
            description=(
                "Report the persistent goal state. Call this exactly once as the final tool "
                "action of every goal turn."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["continue", "ready", "blocked", "completed"],
                    },
                    "summary": {
                        "type": "string",
                        "description": "Concise description of progress and verification evidence.",
                    },
                    "blocker": {
                        "type": "string",
                        "description": "Information required from the user when status is blocked.",
                        "default": "",
                    },
                },
                "required": ["status", "summary"],
            },
            function=record_status,
            visible=False,
        )
        spec = self.runtime.tool_registry._tools["goal_status"]
        self.runtime._tools_for_api = [
            *previous_tools,
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.parameters,
                },
            },
        ]
        self.runtime.approval_manager.READ_ONLY_TOOLS = {
            *previous_read_only,
            "goal_status",
        }

        try:
            goal = self.store.update(
                goal_id,
                session_id=self.runtime.session_store.session_id,
                status="running",
                last_error=None,
            )
            while goal["cycle_count"] < goal["max_cycles"]:
                verifying = goal["status"] == "verifying"
                signal.clear()
                prompt = self._build_prompt(goal, verifying=verifying, note=note)
                note = ""

                try:
                    result = self.runtime.run_turn(prompt)
                except KeyboardInterrupt:
                    self.store.update(
                        goal_id,
                        session_id=self.runtime.session_store.session_id,
                        status="paused",
                        last_error="user interrupted",
                    )
                    raise

                session_id = self.runtime.session_store.session_id
                cycle_count = goal["cycle_count"] + 1
                if not result.get("ok"):
                    error = str(result.get("error") or "unknown turn error")
                    goal = self.store.update(
                        goal_id,
                        session_id=session_id,
                        status="paused",
                        cycle_count=cycle_count,
                        last_error=error,
                    )
                    return {
                        **result,
                        "error": f"Goal paused due to turn error: {error}",
                        "goal_status": goal["status"],
                        "goal": goal,
                    }

                reported = signal.get("status", "continue")
                summary = signal.get("summary") or str(
                    result.get("final_answer") or result.get("content") or ""
                )
                blocker = signal.get("blocker", "")

                if reported == "blocked":
                    goal = self.store.update(
                        goal_id,
                        session_id=session_id,
                        status="blocked",
                        cycle_count=cycle_count,
                        last_summary=summary[:4000],
                        last_error=blocker or None,
                    )
                    return {
                        **result,
                        "goal_status": goal["status"],
                        "goal": goal,
                    }

                if reported == "completed" and verifying:
                    goal = self.store.update(
                        goal_id,
                        session_id=session_id,
                        status="completed",
                        cycle_count=cycle_count,
                        last_summary=summary[:4000],
                        last_error=None,
                    )
                    return {
                        **result,
                        "goal_status": goal["status"],
                        "goal": goal,
                    }

                next_status = (
                    "verifying" if reported in {"ready", "completed"} else "running"
                )
                goal = self.store.update(
                    goal_id,
                    session_id=session_id,
                    status=next_status,
                    cycle_count=cycle_count,
                    last_summary=summary[:4000],
                    last_error=None,
                )

            goal = self.store.update(
                goal_id,
                session_id=self.runtime.session_store.session_id,
                status="paused",
                last_error="maximum goal cycles reached",
            )
            return {
                "ok": False,
                "error": "Goal paused after reaching the maximum number of cycles.",
                "final_answer": "",
                "content": "",
                "goal_status": goal["status"],
                "goal": goal,
            }
        finally:
            self.runtime._tools_for_api = previous_tools
            self.runtime.approval_manager.READ_ONLY_TOOLS = previous_read_only
            self.runtime.tool_registry.unregister("goal_status")

    @staticmethod
    def _build_prompt(goal: dict[str, Any], *, verifying: bool, note: str) -> str:
        context = [
            "You are running in persistent goal mode.",
            f"Goal: {goal['objective']}",
            f"Cycle: {goal['cycle_count'] + 1}/{goal['max_cycles']}",
        ]
        if goal.get("last_summary"):
            context.append(f"Previous progress: {goal['last_summary']}")
        if note:
            context.append(f"Additional user guidance: {note}")

        if verifying:
            instructions = """
Independently verify that the goal is actually complete. Inspect the current workspace and run the relevant checks. Do not accept an earlier claim without evidence.
- Call goal_status(status="completed", ...) only when the goal is fully satisfied and verified.
- Call goal_status(status="continue", ...) when more work or fixes remain.
- Call goal_status(status="blocked", ...) only when user input is required.
"""
        else:
            instructions = """
Continue taking concrete actions with the available tools. Do not stop after only explaining what should be done.
- Call goal_status(status="ready", ...) when implementation is finished and relevant checks have passed; the next cycle will verify it.
- Call goal_status(status="continue", ...) when more work remains.
- Call goal_status(status="blocked", ...) only when user input is required.
- Do not report completed during a work cycle.
"""

        return "\n".join(context) + "\n" + instructions.strip()
