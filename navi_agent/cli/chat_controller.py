from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from rich.markdown import Markdown
from rich.text import Text

from ..tools.approval import ApprovalDecision, UserApprovalChoice
from ..tools.approval_broker import ApprovalBroker
from ..paths import get_navi_home, load_navi_dotenv
from .prompt_ui import NaviPromptSession
from ..runtime.agent import AgentRuntime
from .stream_box import StreamingBox
from .ui import console


class ChatController:
    """Owns the interactive CLI session wiring between prompt UI and runtime."""

    def __init__(
        self,
        *,
        workspace: Path,
        max_steps: int,
        no_splash: bool,
        approval_mode: str,
        resume_session_id: str | None,
        slash_commands: list[str],
        print_live: Callable[..., None],
        print_splash: Callable[[Path, str, str, str | None], None],
        print_agent_event: Callable[..., None],
        render_bottom_toolbar: Callable[[AgentRuntime, Path, dict | None], Any],
        create_prompt_key_bindings: Callable[[AgentRuntime | None], KeyBindings],
        handle_slash_command: Callable[[str, AgentRuntime, Path], bool],
        ask_approval_from_cli: Callable[[ApprovalDecision], UserApprovalChoice],
        result_is_ok: Callable[[Any], bool],
        result_error: Callable[[Any], str],
    ) -> None:
        self.workspace = workspace
        self.max_steps = max_steps
        self.no_splash = no_splash
        self.approval_mode = approval_mode
        self.resume_session_id = resume_session_id
        self.slash_commands = slash_commands
        self.print_live = print_live
        self.print_splash = print_splash
        self.print_agent_event = print_agent_event
        self.render_bottom_toolbar = render_bottom_toolbar
        self.create_prompt_key_bindings = create_prompt_key_bindings
        self.handle_slash_command = handle_slash_command
        self.ask_approval_from_cli = ask_approval_from_cli
        self.result_is_ok = result_is_ok
        self.result_error = result_error

        self.runtime: AgentRuntime | None = None
        self.prompt_session: NaviPromptSession | None = None
        self.approval_broker: ApprovalBroker | None = None
        self.stream_box: StreamingBox | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.cancel_notice_printed = False
        self.timer: dict[str, Any] = {"start": None, "frozen": 0.0}

    async def run(self) -> None:
        load_navi_dotenv()
        self.loop = asyncio.get_running_loop()
        self.workspace = self.workspace.resolve()

        self.stream_box = StreamingBox(self.print_live)

        def approval_handler(decision: ApprovalDecision) -> UserApprovalChoice:
            if self.approval_broker is None:
                return self.ask_approval_from_cli(decision)
            return self.approval_broker.request(decision)

        def cancel_approval() -> None:
            if self.approval_broker is not None:
                self.approval_broker.cancel_current()

        approval_handler.cancel_current = cancel_approval  # type: ignore[attr-defined]

        self.runtime = AgentRuntime(
            workspace=self.workspace,
            max_steps=self.max_steps,
            event_handler=lambda e: self.print_agent_event(e, box=self.stream_box),
            approval_mode=self.approval_mode,
            approval_handler=approval_handler,
            resume_session_id=self.resume_session_id,
            on_output=self.print_live,
        )

        navi_home = get_navi_home()
        self.prompt_session = NaviPromptSession(
            history_path=navi_home / "chat_history.txt",
            completer=WordCompleter(
                self.slash_commands,
                ignore_case=True,
                WORD=True,
            ),
            key_bindings=self.create_prompt_key_bindings(self.runtime),
            bottom_toolbar=lambda: self.render_bottom_toolbar(
                self.runtime, self.workspace, self.timer
            ),
            on_cancel=self.handle_cancel,
            on_approval_response=self.handle_approval_response,
        )

        self.approval_broker = ApprovalBroker(
            on_request=lambda decision: self._call_ui(
                lambda: self.prompt_session.show_approval(decision)
            ),
            on_clear=lambda: self._call_ui(self.prompt_session.clear_approval),
        )

        if not self.no_splash and not self.resume_session_id:
            self.print_splash(
                self.workspace,
                self.runtime.router.model_name,
                self.approval_mode,
                self.runtime.session_store.session_id,
            )

        await self.prompt_session.run_session(on_submit=self.process_message)

        sid = self.runtime.session_store.session_id
        console.print(f"[dim]To resume this session: navi --resume {sid}[/dim]")

    async def process_message(self, text: str) -> None:
        runtime = self._runtime()
        prompt_session = self._prompt_session()
        stream_box = self._stream_box()

        if text.strip() == "/model":
            self.open_model_picker()
            return

        if self.handle_slash_command(
            command=text,
            runtime=runtime,
            workspace=self.workspace,
        ):
            return

        if runtime.reviewer.pending_message:
            msg = runtime.reviewer.pending_message
            runtime.reviewer.pending_message = None
            self.print_live(f"[dim]💾 {msg}[/dim]")

        self.print_live()
        self.print_live(Text(f"> {text}", style="#87CEEB"))
        self.print_live()

        import time as _time

        self.timer["start"] = _time.time()
        self.timer["frozen"] = 0.0
        prompt_session.invalidate()

        self.cancel_notice_printed = False
        prompt_session.begin_running()
        stream_box.reset()

        async def _tick_toolbar() -> None:
            while self.timer["start"] is not None:
                prompt_session.invalidate()
                await asyncio.sleep(1)

        tick_task = asyncio.ensure_future(_tick_toolbar())

        def runner() -> dict[str, Any]:
            try:
                return runtime.run_turn(text)
            except KeyboardInterrupt:
                return {
                    "ok": False,
                    "error": "用户中断",
                    "final_answer": "",
                    "content": "",
                }

        result = await asyncio.get_running_loop().run_in_executor(None, runner)

        if self.timer["start"] is not None:
            self.timer["frozen"] = max(0.0, _time.time() - self.timer["start"])
            self.timer["start"] = None
        tick_task.cancel()
        prompt_session.invalidate()

        stream_box.close_all()
        prompt_session.end_running()

        if prompt_session.force_exit:
            prompt_session.exit(result="exit")
            raise EOFError("Force exit (double Ctrl+C)")

        if not stream_box.had_output:
            answer = result.get("final_answer") or result.get("content") or ""
            if answer:
                self.print_live()
                self.print_live(Markdown(answer))

        if prompt_session.cancel_requested:
            self.print_live("[yellow]Interrupted.[/yellow]")

        if not self.result_is_ok(result):
            self.print_live(f"[red]{self.result_error(result)}[/red]")

    def open_model_picker(self) -> None:
        runtime = self._runtime()
        prompt_session = self._prompt_session()

        info = runtime.get_model_info()
        providers = info["providers"]
        if not providers:
            self.print_live("[yellow]No providers configured. Edit ~/.navi/config.json[/yellow]")
            return

        def on_provider_selected(provider: str) -> list[str]:
            models = runtime.router.list_models(provider)
            return list(models.keys())

        def on_model_selected(provider: str, model: str) -> None:
            if runtime.switch_model(provider, model):
                self.print_live(f"[green]Switched to {provider} / {model}[/green]")
            else:
                self.print_live(f"[red]Failed to switch to {provider} / {model}[/red]")

        prompt_session.open_model_picker(
            providers=providers,
            current_provider=info["current_provider"],
            current_model=info["current_model"],
            on_provider_selected=on_provider_selected,
            on_model_selected=on_model_selected,
        )

    def handle_cancel(self) -> None:
        runtime = self.runtime
        if runtime is not None:
            self.loop.run_in_executor(None, runtime.interrupt)
        if not self.cancel_notice_printed:
            self.cancel_notice_printed = True
            self.print_live("[yellow]Interrupt requested; waiting for current operation...[/yellow]")

    def handle_approval_response(self, choice: UserApprovalChoice) -> None:
        if self.approval_broker is not None:
            self.approval_broker.resolve(choice)

    def _call_ui(self, callback: Callable[[], None]) -> None:
        if self.loop is None:
            callback()
            return
        self.loop.call_soon_threadsafe(callback)

    def _runtime(self) -> AgentRuntime:
        if self.runtime is None:
            raise RuntimeError("ChatController runtime is not initialized.")
        return self.runtime

    def _prompt_session(self) -> NaviPromptSession:
        if self.prompt_session is None:
            raise RuntimeError("ChatController prompt session is not initialized.")
        return self.prompt_session

    def _stream_box(self) -> StreamingBox:
        if self.stream_box is None:
            raise RuntimeError("ChatController stream box is not initialized.")
        return self.stream_box
