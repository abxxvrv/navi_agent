import asyncio
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.input.defaults import create_pipe_input
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.output import DummyOutput

from navi_agent.cli.chat_controller import ChatController
from navi_agent.cli.prompt_ui import NaviPromptSession
from navi_agent.cli.terminal_output import TerminalOutput


def _make_prompt(pipe_input, tmp_path):
    return NaviPromptSession(
        history_path=tmp_path / "history.txt",
        completer=None,
        key_bindings=KeyBindings(),
        bottom_toolbar=lambda: [("", "toolbar")],
        image_dir=tmp_path / "images",
        input=pipe_input,
        output=DummyOutput(),
    )


def test_empty_text_with_image_submits_and_clears(tmp_path):
    image = tmp_path / "a.png"
    image.write_bytes(b"png")

    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input, tmp_path)
            assert prompt.attach_image_path(image)
            submitted = []

            async def on_submit(text, images):
                submitted.append((text, images))
                prompt.exit()

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)
            pipe_input.send_text("\r")
            await asyncio.wait_for(task, timeout=1.0)

            assert submitted == [("", [image])]
            assert prompt._attached_images == []

    asyncio.run(run())


def test_non_empty_bracketed_paste_does_not_attach_clipboard_image(tmp_path, monkeypatch):
    calls = []

    def fake_save(path: Path) -> bool:
        calls.append(path)
        return True

    monkeypatch.setattr("navi_agent.cli.prompt_ui.save_clipboard_image", fake_save)

    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input, tmp_path)
            submitted = []

            async def on_submit(text, images):
                submitted.append((text, images))

            task = asyncio.create_task(prompt.run_session(on_submit=on_submit))
            await asyncio.sleep(0.05)
            pipe_input.send_text("\x1b[200~hello\x1b[201~")
            await asyncio.sleep(0.1)

            assert calls == []
            assert prompt._attached_images == []
            assert prompt._buffer.text == "hello"

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_bracketed_paste_image_path_attaches_image(tmp_path):
    image = tmp_path / "qq_thumb.png"
    image.write_bytes(b"png")

    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input, tmp_path)

            task = asyncio.create_task(prompt.run_session(on_submit=lambda text, images: None))
            await asyncio.sleep(0.05)
            pipe_input.send_text(f"\x1b[200~{image}\x1b[201~")
            await asyncio.sleep(0.1)

            assert prompt._attached_images == [image]
            assert prompt._buffer.text == ""

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_bracketed_paste_uses_event_data_for_image_path(tmp_path):
    image_dir = tmp_path / "QQ接收消息"
    image_dir.mkdir()
    image = image_dir / "qq_thumb.png"
    image.write_bytes(b"png")
    bad_path = str(image).replace("QQ接收消息", "QQ������Ϣ")

    async def run():
        with create_pipe_input() as pipe_input:
            prompt = _make_prompt(pipe_input, tmp_path)

            task = asyncio.create_task(prompt.run_session(on_submit=lambda text, images: None))
            await asyncio.sleep(0.05)
            pipe_input.send_text(f"\x1b[200~{bad_path}\x1b[201~")
            await asyncio.sleep(0.1)

            assert prompt._attached_images == []
            assert prompt._buffer.text == bad_path

            prompt.exit()
            await asyncio.wait_for(task, timeout=1.0)

    asyncio.run(run())


def test_paste_slash_command_attaches_clipboard_image(tmp_path):
    controller = ChatController.__new__(ChatController)
    prompt = SimpleNamespace(attached=False)

    def attach_clipboard_image():
        prompt.attached = True
        return True

    prompt.attach_clipboard_image = attach_clipboard_image
    controller.runtime = object()
    controller.prompt_session = prompt
    controller.stream_box = object()
    controller.workspace = tmp_path
    controller.handle_slash_command = lambda **kwargs: False

    asyncio.run(controller.process_message("/paste"))

    assert prompt.attached is True


def test_image_slash_command_attaches_path(tmp_path):
    image = tmp_path / "b.jpg"
    image.write_bytes(b"jpg")
    controller = ChatController.__new__(ChatController)
    attached = []
    prompt = SimpleNamespace(
        attach_clipboard_image=lambda: False,
        attach_image_path=lambda path: attached.append(path) or True,
    )
    controller.runtime = object()
    controller.prompt_session = prompt
    controller.stream_box = object()
    controller.workspace = tmp_path
    controller.handle_slash_command = lambda **kwargs: False

    asyncio.run(controller.process_message(f"/image {image}"))

    assert attached == [image]


def test_process_message_prints_submitted_preview_once_and_expands_loop(tmp_path):
    controller = ChatController.__new__(ChatController)
    calls = []

    class Prompt:
        cancel_requested = False
        force_exit = False
        is_running = False

        def invalidate(self):
            pass

        def begin_running(self):
            self.is_running = True

        def end_running(self):
            self.is_running = False

    class StreamBox:
        had_output = True

        def reset(self):
            pass

        def close_all(self):
            pass

    class Runtime:
        reviewer = SimpleNamespace(pending_message=None)

        def __init__(self):
            self.inputs = []
            self.goal_runner = SimpleNamespace(drive=self.run_turn)

        def run_turn(self, text, image_paths=None):
            self.inputs.append(text)
            return {"ok": True, "final_answer": "", "content": ""}

    controller.runtime = Runtime()
    controller.prompt_session = Prompt()
    controller.stream_box = StreamBox()
    controller.workspace = tmp_path
    controller.timer = {"start": None, "frozen": 0.0}
    controller.cancel_notice_printed = False
    controller.output = TerminalOutput(
        lambda *args, **kwargs: calls.append(args),
        lambda *args, **kwargs: None,
    )
    controller.handle_slash_command = lambda **kwargs: False
    controller.result_is_ok = lambda result: True
    controller.result_error = lambda result: "error"

    asyncio.run(controller.process_message("/loop every 5 minutes check deploy"))

    assert len(calls) == 1
    assert "Do NOT execute the prompt inline" in controller.runtime.inputs[0]
    assert "instead of guessing" in controller.runtime.inputs[0]
    assert "every 5 minutes check deploy" in controller.runtime.inputs[0]
