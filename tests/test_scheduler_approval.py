from navi_agent.tools.approval import (
    ApprovalAction,
    ApprovalManager,
    RiskLevel,
    UserApprovalChoice,
)


def test_scheduler_create_requires_approval_except_open_or_session(tmp_path):
    args = {"interval": "5m", "prompt": "check deploy"}
    for mode in ("normal", "strict"):
        manager = ApprovalManager(mode=mode, workspace=tmp_path)
        decision = manager.check_tool_call("scheduler_create", args)

        assert decision.action is ApprovalAction.ASK
        assert decision.risk is RiskLevel.RISKY
        assert decision.approval_key == "tool:scheduler_create"

        manager.resolve_user_choice(decision, UserApprovalChoice.ALLOW_SESSION)
        assert manager.check_tool_call("scheduler_create", args).action is ApprovalAction.ALLOW

    assert (
        ApprovalManager(mode="open", workspace=tmp_path)
        .check_tool_call("scheduler_create", args)
        .action
        is ApprovalAction.ALLOW
    )
