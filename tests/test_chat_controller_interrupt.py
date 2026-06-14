from navi_agent.cli.chat_controller import ChatController


def test_sigint_routes_to_prompt_when_running(monkeypatch, tmp_path):
    trace_path = tmp_path / "interrupt_trace.log"
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE", "1")
    monkeypatch.setenv("NAVI_INTERRUPT_TRACE_PATH", str(trace_path))

    previous_calls = []
    routed_calls = []
    installed = {}

    def previous_handler(signum, frame):
        previous_calls.append(signum)

    def fake_getsignal(signum):
        return previous_handler

    def fake_signal(signum, handler):
        installed["handler"] = handler

    monkeypatch.setattr("signal.getsignal", fake_getsignal)
    monkeypatch.setattr("signal.signal", fake_signal)

    class FakePromptSession:
        can_handle_interrupt_signal = True
        is_running = True
        cancel_requested = False
        force_exit = False

        def handle_interrupt_signal(self):
            routed_calls.append(True)

    controller = object.__new__(ChatController)
    controller.prompt_session = FakePromptSession()
    controller.runtime = object()
    controller.loop = None

    restore = controller._install_sigint_trace()
    installed["handler"](2, None)

    assert routed_calls == [True]
    assert previous_calls == []

    restore()
