from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.key_binding import KeyBindings
from rich.markup import escape

from ..integrations.mcp_client import shutdown_mcp_servers
from ..tools.approval import ApprovalDecision, UserApprovalChoice
from ..tools.approval_broker import ApprovalBroker
from ..paths import get_navi_home, load_navi_dotenv
from .prompt_ui import NaviPromptSession
from ..runtime.agent import AgentRuntime
from ..runtime.goal import parse_goal_command
from .interrupt_trace import interrupt_trace_enabled, trace_interrupt
from .paste_collapse import expand_paste_references
from .paste_trace import summarize_text, trace_paste
from .stream_box import StreamingBox
from .terminal_output import TerminalOutput
from .ui import _format_args, console


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
        plugin_dirs: list[Path] | None,
        slash_commands: list[str],
        print_live: Callable[..., None],
        print_splash: Callable[[Path, str, str, str | None], None],
        print_agent_event: Callable[..., None],
        render_bottom_toolbar: Callable[[AgentRuntime, Path, dict | None], Any],
        create_prompt_key_bindings: Callable[[AgentRuntime | None], KeyBindings],
        handle_slash_command: Callable[..., bool],
        ask_approval_from_cli: Callable[[ApprovalDecision], UserApprovalChoice],
        result_is_ok: Callable[[Any], bool],
        result_error: Callable[[Any], str],
    ) -> None:
        self.workspace = workspace
        self.max_steps = max_steps
        self.no_splash = no_splash
        self.approval_mode = approval_mode
        self.resume_session_id = resume_session_id
        self.plugin_dirs = plugin_dirs
        self.slash_commands = slash_commands
        self.output = TerminalOutput(print_live, print_agent_event)
        self.print_splash = print_splash
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
        self.current_tool_call_id: str | None = None
        self.pending_monitor_events: list[dict[str, Any]] = []
        self.monitor_notification_queued = False
        self.timer: dict[str, Any] = {"start": None, "frozen": 0.0}

    async def run(self) -> None:
        load_navi_dotenv()
        self.loop = asyncio.get_running_loop()
        self.workspace = self.workspace.resolve()

        self.stream_box = StreamingBox(self.output.raw)

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
            event_handler=self.handle_runtime_event,
            approval_mode=self.approval_mode,
            approval_handler=approval_handler,
            resume_session_id=self.resume_session_id,
            on_output=self.output.raw,
            plugin_dirs=self.plugin_dirs,
        )

        navi_home = get_navi_home()
        slash_commands = list(self.slash_commands)
        if "/goal" not in slash_commands:
            slash_commands.append("/goal")
        slash_commands.extend(
            f"/{name}"
            for name in sorted(self.runtime.plugin_commands)
            if f"/{name}" not in slash_commands
        )
        self.prompt_session = NaviPromptSession(
            history_path=navi_home / "chat_history.txt",
            completer=WordCompleter(
                slash_commands,
                ignore_case=True,
                WORD=True,
            ),
            key_bindings=self.create_prompt_key_bindings(self.runtime),
            bottom_toolbar=lambda: self.render_bottom_toolbar(
                self.runtime, self.workspace, self.timer
            ),
            image_dir=navi_home / "images",
            on_cancel=self.handle_cancel,
            on_background=lambda: self.runtime.task_manager.background_current(
                self.current_tool_call_id
            ),
            on_approval_response=self.handle_approval_response,
        )

        self.approval_broker = ApprovalBroker(
            on_request=lambda decision: self._call_ui(
                lambda: self.prompt_session.show_approval(decision)
            ),
            on_clear=lambda: self._call_ui(self.prompt_session.clear_approval),
        )
        self.prompt_session.approval_broker = self.approval_broker
        self.runtime.scheduler.start()

        restore_sigint_trace = self._install_sigint_trace()
        try:
            if not self.no_splash and not self.resume_session_id:
                self.print_splash(
                    self.workspace,
                    self.runtime.router.model_name,
                    self.approval_mode,
                    self.runtime.session_store.session_id,
                )

            await self.prompt_session.run_session(on_submit=self.process_message)
        finally:
            restore_sigint_trace()
            try:
                self.runtime.close()
            finally:
                shutdown_mcp_servers()

        sid = self.runtime.session_store.session_id
        console.print(f"[dim]To resume this session: navi --resume {sid}[/dim]")

    def handle_runtime_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "task_backgrounded":
            task = event["task"]
            self.output.notice(
                f"[dim]Task {task['task_id']} moved to the background.[/dim]"
            )
            return

        if event_type == "task_completed":
            task = event["task"]
            prompt = self.prompt_session
            if prompt is not None:
                if task["task_type"] == "subagent":
                    text = (
                        "<system-reminder>\n"
                        f"Subagent {task['task_id']} ({task['description']}) finished "
                        f"with status {task['status']}. Use get_task_output to read its result.\n"
                        "</system-reminder>"
                    )
                else:
                    text = (
                        "<system-reminder>\n"
                        f"Background {task['task_type']} {task['task_id']} finished with "
                        f"status {task['status']} and exit code {task['exit_code']}. "
                        "Use get_task_output if the output is needed.\n"
                        "</system-reminder>"
                    )
                self._call_ui(
                    lambda: prompt._idle_queue.put_nowait(
                        (text, [], f"task:{task['task_id']}")
                    )
                )
            return

        if event_type == "monitor_event":
            self._call_ui(lambda: self._queue_monitor_event(event))
            return

        if event_type == "scheduled_prompt":
            prompt = self.prompt_session
            if prompt is not None:
                text = (
                    "<system-reminder>\n"
                    f"Scheduled task {event['task_id']} fired ({event['human_schedule']}).\n"
                    f"{event['prompt']}\n"
                    "</system-reminder>"
                )
                self._call_ui(
                    lambda: prompt._idle_queue.put_nowait(
                        (text, [], f"scheduler:{event['task_id']}")
                    )
                )
            return

        if event_type == "approval_batch_done":
            prompt = self.prompt_session
            if prompt is not None:
                self._call_ui(lambda: prompt.clear_approval(clear_history=True))
            return

        if event_type == "tool_start":
            if self.stream_box is not None and self.stream_box.has_output:
                self.stream_box.close_all()
            name = str(event.get("tool_name") or "tool")
            if name in {"bash", "powershell"}:
                self.current_tool_call_id = event.get("tool_call_id")
            detail = _format_args(event.get("tool_args") or {})
            prompt = self.prompt_session
            if prompt is not None:
                self._call_ui(lambda: prompt.set_tool_status(name, detail))
            return

        if event_type in {"tool_result", "tool_error"}:
            self.output.agent_event(event, box=self.stream_box)
            name = str(event.get("tool_name") or "tool")
            prompt = self.prompt_session
            if prompt is not None:
                self._call_ui(lambda: prompt.clear_tool_status(name))
            if event.get("tool_call_id") == self.current_tool_call_id:
                self.current_tool_call_id = None
            return

        self.output.agent_event(event, box=self.stream_box)

    async def process_message(
        self,
        text: str,
        image_paths: list[Path] | None = None,
        origin: str = "user",
    ) -> None:
        image_paths = image_paths or []
        if origin == "monitor":
            events = self.pending_monitor_events
            self.pending_monitor_events = []
            self.monitor_notification_queued = False
            if not events:
                return
            text = (
                "<system-reminder>\n"
                + "\n\n".join(
                    f"Monitor {event['task_id']} ({event['description']}) reported:\n"
                    f"{event['output']}"
                    for event in events
                )
                + "\n</system-reminder>"
            )
        trace_paste(
            "process_message_start",
            text_summary=summarize_text(text),
        )
        runtime = self._runtime()
        prompt_session = self._prompt_session()
        stream_box = self._stream_box()

        if origin == "user" and not image_paths and text.strip() == "/model":
            self.open_model_picker()
            return

        stripped = text.strip()
        loop_input: str | None = None
        plugin_input: str | None = None
        if origin == "user" and stripped == "/loop":
            self.output.notice("[yellow]Usage: /loop <interval> <prompt>[/yellow]")
            return
        if origin == "user" and stripped.startswith("/loop "):
            loop_input = (
                "Create a recurring scheduled task from this request. Derive the interval as "
                "<number><unit> (s, m, h, or d); if it is missing or ambiguous, ask the user "
                "instead of guessing. Intervals below 60 seconds are clamped to 60 seconds. "
                "Call scheduler_create with recurring=true and fire_immediately=true. Do NOT "
                "execute the prompt inline; the scheduler will fire it immediately. After the "
                "tool succeeds, confirm the cadence, 7-day expiry, and cancellation ID. Request: "
                + stripped[len("/loop ") :].strip()
            )
        if origin == "user" and not image_paths and stripped == "/paste":
            if not prompt_session.attach_clipboard_image():
                self.output.notice("[yellow]No image found in clipboard.[/yellow]")
            return

        if origin == "user" and not image_paths and stripped.startswith("/image "):
            path_text = stripped[len("/image "):].strip().strip('"')
            image_path = Path(path_text)
            if path_text and not image_path.is_absolute():
                image_path = self.workspace / image_path
            if not path_text or not prompt_session.attach_image_path(image_path):
                self.output.notice(f"[yellow]Cannot attach image: {path_text or '<empty>'}[/yellow]")
            return

        if origin == "user" and not image_paths and stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            command_body = runtime.plugin_commands.get(parts[0][1:])
            if command_body is not None:
                command_body = command_body.replace(
                    "${SESSION_ID}", runtime.session_store.session_id
                ).replace("${CLAUDE_SESSION_ID}", runtime.session_store.session_id)
                arguments = parts[1] if len(parts) > 1 else ""
                argv = arguments.split()
                plugin_input = command_body
                arguments_substituted = False
                max_index = max(len(argv), 1)
                for index in range(max_index + 19, -1, -1):
                    token = f"$ARGUMENTS[{index}]"
                    if token in plugin_input:
                        plugin_input = plugin_input.replace(
                            token,
                            argv[index] if index < len(argv) else "",
                        )
                        arguments_substituted = True
                for index in range(max_index + 19, -1, -1):
                    token = f"${index}"
                    if re.search(rf"{re.escape(token)}(?!\d)", plugin_input):
                        plugin_input = re.sub(
                            rf"{re.escape(token)}(?!\d)",
                            argv[index] if index < len(argv) else "",
                            plugin_input,
                        )
                        arguments_substituted = True
                if "$ARGUMENTS" in plugin_input:
                    plugin_input = plugin_input.replace("$ARGUMENTS", arguments)
                    arguments_substituted = True
                if arguments and not arguments_substituted:
                    plugin_input += f"\n\n**ARGUMENTS:** {arguments}"

        goal_runner = runtime.goal_runner
        goal_input: str | None = None
        goal_command = (
            parse_goal_command(stripped)
            if origin == "user" and not image_paths
            else None
        )
        if goal_command is not None:
            action, argument = goal_command
            command_result = goal_runner.apply_command(action, argument)
            goal_input = command_result["run_input"]
            if goal_input is None:
                style = "green" if command_result["ok"] else "yellow"
                self.print_live(
                    f"[{style}]{escape(str(command_result['message']))}[/{style}]"
                )
                return
            self.print_live(
                f"[green]{escape(str(command_result['message']))}[/green]"
            )

        if (
            origin == "user"
            and goal_command is None
            and plugin_input is None
            and not image_paths
            and self.handle_slash_command(
                command=text,
                runtime=runtime,
                workspace=self.workspace,
                printer=self.output.raw,
            )
        ):
            return

        display_text = text
        runtime_text = (
            goal_input
            if goal_input is not None
            else loop_input
            if loop_input is not None
            else plugin_input
            if plugin_input is not None
            else expand_paste_references(text)
            if origin == "user"
            else text
        )
        if runtime_text != display_text:
            trace_paste("paste_reference_expanded", text_summary=summarize_text(display_text))

        if runtime.reviewer.pending_message:
            msg = runtime.reviewer.pending_message
            runtime.reviewer.pending_message = None
            self.output.notice(f"[dim]💾 {msg}[/dim]")

        if origin == "user":
            self.output.user_message(display_text, image_paths)
        else:
            self.output.notice(f"[dim]Running {escape(origin)} notification...[/dim]")

        import time as _time

        self.timer["start"] = _time.time()
        self.timer["frozen"] = 0.0
        prompt_session.invalidate()

        self.cancel_notice_printed = False
        trace_paste(
            "process_message_before_begin_running",
            text_summary=summarize_text(text),
            prompt_is_running=prompt_session.is_running,
        )
        prompt_session.begin_running()
        trace_paste(
            "process_message_after_begin_running",
            text_summary=summarize_text(text),
            prompt_is_running=prompt_session.is_running,
        )
        stream_box.reset()

        async def _tick_toolbar() -> None:
            while self.timer["start"] is not None:
                prompt_session.invalidate()
                await asyncio.sleep(1)

        tick_task = asyncio.ensure_future(_tick_toolbar())

        def runner() -> dict[str, Any]:
            try:
                if origin == "user":
                    return goal_runner.drive(runtime_text, image_paths=image_paths)
                return runtime.run_turn(runtime_text)
            except KeyboardInterrupt:
                return {
                    "ok": False,
                    "error": "用户中断",
                    "final_answer": "",
                    "content": "",
                }

        try:
            result = await asyncio.get_running_loop().run_in_executor(None, runner)
        finally:
            if origin.startswith("scheduler:"):
                runtime.scheduler.complete(origin.split(":", 1)[1])

        if self.timer["start"] is not None:
            self.timer["frozen"] = max(0.0, _time.time() - self.timer["start"])
            self.timer["start"] = None
        tick_task.cancel()
        prompt_session.invalidate()

        stream_box.close_all()
        prompt_session.end_running()

        trace_paste(
            "process_message_result",
            text_summary=summarize_text(text),
            prompt_is_running=prompt_session.is_running,
            cancel_requested=prompt_session.cancel_requested,
            force_exit=prompt_session.force_exit,
            result_ok=self.result_is_ok(result),
        )

        if prompt_session.force_exit:
            prompt_session.exit(result="exit")
            raise EOFError("Force exit (double Ctrl+C)")

        if not stream_box.had_output:
            answer = result.get("final_answer") or result.get("content") or ""
            self.output.assistant(answer)

        goal_status = result.get("goal_status")
        goal = result.get("goal") or {}
        if goal_status == "complete":
            self.print_live(f"[green]Goal {goal.get('goal_id', '')} completed.[/green]")
        elif goal_status == "blocked":
            self.print_live(f"[yellow]Goal {goal.get('goal_id', '')} is blocked.[/yellow]")
        elif goal_status == "paused" and not prompt_session.cancel_requested:
            self.print_live(f"[yellow]Goal {goal.get('goal_id', '')} paused.[/yellow]")

        if prompt_session.cancel_requested:
            self.output.notice("[yellow]Interrupted.[/yellow]")

        if not self.result_is_ok(result):
            self.output.error(self.result_error(result))

    def open_model_picker(self) -> None:
        runtime = self._runtime()
        prompt_session = self._prompt_session()

        info = runtime.get_model_info()
        providers = info["providers"]
        if not providers:
            self.output.notice("[yellow]No providers configured. Edit ~/.navi/config.json[/yellow]")
            return

        def on_provider_selected(provider: str) -> list[str]:
            models = runtime.router.list_models(provider)
            return list(models.keys())

        def on_model_selected(provider: str, model: str) -> None:
            if runtime.switch_model(provider, model):
                self.output.notice(f"[green]Switched to {provider} / {model}[/green]")
            else:
                self.output.error(f"Failed to switch to {provider} / {model}")

        prompt_session.open_model_picker(
            providers=providers,
            current_provider=info["current_provider"],
            current_model=info["current_model"],
            on_provider_selected=on_provider_selected,
            on_model_selected=on_model_selected,
        )

    def handle_cancel(self) -> None:
        runtime = self.runtime
        trace_interrupt(
            "chat_handle_cancel",
            runtime_exists=runtime is not None,
            loop_exists=self.loop is not None,
            cancel_notice_printed=self.cancel_notice_printed,
        )
        if runtime is not None:
            self.loop.run_in_executor(None, runtime.interrupt)
            trace_interrupt("chat_runtime_interrupt_scheduled")
        if not self.cancel_notice_printed:
            self.cancel_notice_printed = True
            self.output.notice("[yellow]Interrupt requested; waiting for current operation...[/yellow]")

    def handle_approval_response(self, choice: UserApprovalChoice) -> None:
        if self.approval_broker is not None:
            self.approval_broker.resolve(choice)

    def _call_ui(self, callback: Callable[[], None]) -> None:
        if self.loop is None:
            callback()
            return
        self.loop.call_soon_threadsafe(callback)

    def _queue_monitor_event(self, event: dict[str, Any]) -> None:
        prompt = self.prompt_session
        if prompt is None:
            return
        self.pending_monitor_events.append(event)
        if len(self.pending_monitor_events) > 50:
            del self.pending_monitor_events[:-50]
        if self.monitor_notification_queued:
            return
        self.monitor_notification_queued = True
        prompt._idle_queue.put_nowait(("", [], "monitor"))

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

    def _install_sigint_trace(self) -> Callable[[], None]:
        try:
            import signal
            import threading

            if threading.current_thread() is not threading.main_thread():
                trace_interrupt("sigint_trace_not_installed", reason="not_main_thread")
                return lambda: None

            previous_handler = signal.getsignal(signal.SIGINT)

            def _sigint_trace_handler(signum, frame) -> None:
                prompt_session = self.prompt_session
                prompt_can_handle = (
                    prompt_session is not None
                    and getattr(prompt_session, "can_handle_interrupt_signal", False)
                )
                trace_interrupt(
                    "sigint",
                    signum=signum,
                    prompt_exists=prompt_session is not None,
                    prompt_running=getattr(prompt_session, "is_running", None),
                    prompt_cancel_requested=getattr(prompt_session, "cancel_requested", None),
                    prompt_force_exit=getattr(prompt_session, "force_exit", None),
                    prompt_can_handle=prompt_can_handle,
                    runtime_exists=self.runtime is not None,
                )
                if prompt_can_handle and prompt_session is not None:
                    loop = self.loop
                    if loop is not None:
                        loop.call_soon_threadsafe(prompt_session.handle_interrupt_signal)
                    else:
                        prompt_session.handle_interrupt_signal()
                    trace_interrupt("sigint_routed_to_prompt")
                    return

                if callable(previous_handler):
                    previous_handler(signum, frame)
                    return
                if previous_handler == signal.SIG_IGN:
                    return
                raise KeyboardInterrupt()

            signal.signal(signal.SIGINT, _sigint_trace_handler)
            trace_interrupt("sigint_trace_installed", previous_handler=repr(previous_handler))
        except Exception as exc:
            trace_interrupt("sigint_trace_install_failed", error=repr(exc))
            return lambda: None

        def _restore() -> None:
            try:
                signal.signal(signal.SIGINT, previous_handler)
                trace_interrupt("sigint_trace_restored", previous_handler=repr(previous_handler))
            except Exception as exc:
                trace_interrupt("sigint_trace_restore_failed", error=repr(exc))

        return _restore
