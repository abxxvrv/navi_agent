import threading
import time

from navi_agent.tools.approval import UserApprovalChoice
from navi_agent.tools.approval_broker import ApprovalBroker, ApprovalCancelled


def test_approval_broker_resolves_choice():
    shown = []
    cleared = []
    broker = ApprovalBroker(
        on_request=shown.append,
        on_clear=lambda: cleared.append(True),
        default_timeout=1,
    )

    result = {}

    def wait_for_approval():
        result["choice"] = broker.request({"tool": "run_command"})

    thread = threading.Thread(target=wait_for_approval)
    thread.start()

    deadline = time.monotonic() + 1
    while not shown and time.monotonic() < deadline:
        time.sleep(0.01)

    broker.resolve(UserApprovalChoice.ALLOW_SESSION)
    thread.join(timeout=1)

    assert result["choice"] is UserApprovalChoice.ALLOW_SESSION
    assert shown == [{"tool": "run_command"}]
    assert cleared == [True]


def test_approval_broker_timeout_rejects():
    broker = ApprovalBroker(
        on_request=lambda _decision: None,
        on_clear=lambda: None,
        default_timeout=0.01,
    )

    assert broker.request({"tool": "run_command"}) is UserApprovalChoice.REJECT


def test_approval_broker_cancel_raises_interrupt():
    shown = []
    cleared = []
    broker = ApprovalBroker(
        on_request=shown.append,
        on_clear=lambda: cleared.append(True),
        default_timeout=1,
    )

    result = {}

    def wait_for_approval():
        try:
            broker.request({"tool": "run_command"})
        except ApprovalCancelled as exc:
            result["error"] = exc

    thread = threading.Thread(target=wait_for_approval)
    thread.start()

    deadline = time.monotonic() + 1
    while not shown and time.monotonic() < deadline:
        time.sleep(0.01)

    broker.cancel_current()
    thread.join(timeout=1)

    assert isinstance(result["error"], ApprovalCancelled)
    assert cleared == [True]
