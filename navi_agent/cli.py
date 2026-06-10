from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import typer
from .paths import load_navi_dotenv
from prompt_toolkit.application.current import get_app
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import run_in_terminal
from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text

from .approval import ApprovalDecision, UserApprovalChoice
from .paths import get_navi_home
from .prompt_ui import NaviPromptSession
from .runtime import AgentRuntime
from .ui import NaviStreamView, approval_panel, console, format_context_status
from .stream_box import StreamingBox


APP_NAME = "Navi"
VERSION = "0.1.0"

DEFAULT_MAX_STEPS = 120
DEFAULT_APPROVAL_MODE = "normal"
APPROVAL_MODES = ["strict", "normal", "open"]
SLASH_COMMANDS = [
    "/help",
    "/clear",
    "/tools",
    "/skills",
    "/sessions",
    "/model",
    "/approval",
    "/exit",
    "/quit",
]

NAVI_LOGO = r"""
███╗   ██╗ █████╗ ██╗   ██╗██╗
████╗  ██║██╔══██╗██║   ██║██║
██╔██╗ ██║███████║██║   ██║██║
██║╚██╗██║██╔══██║╚██╗ ██╔╝██║
██║ ╚████║██║  ██║ ╚████╔╝ ██║
╚═╝  ╚═══╝╚═╝  ╚═╝  ╚═══╝  ╚═╝
"""

app = typer.Typer(
    name="navi",
    help="Navi - your local project navigator.",
    no_args_is_help=False,
    add_completion=False,
)

# =========================
# UI
# =========================

def run_with_stream_view(runtime: AgentRuntime, runner):
    view = NaviStreamView()
    original_event_handler = runtime.event_handler
    original_on_output = runtime.on_output
    runtime.event_handler = view.handle_event
    runtime.on_output = view.handle_output
    try:
        with view:
            return runner()
    finally:
        runtime.event_handler = original_event_handler
        runtime.on_output = original_on_output



def _print_through_patch(renderable: Any) -> None:
    """Print a Rich renderable while prompt_toolkit's patch_stdout is active.

    The module-level `console` was created before patch_stdout replaced sys.stdout,
    so writes through it would bypass the proxy.  We create a fresh Console that
    sees the current sys.stdout (the proxy under patch_stdout, or the real stdout
    otherwise) so output lands in the right place.
    """
    import sys

    fresh = Console(file=sys.stdout, highlight=False, force_terminal=True, color_system=console.color_system)
    fresh.print(renderable)


def _build_welcome_renderable(
    workspace: Path,
    model: str,
    approval_mode: str,
    session_id: str | None = None,
) -> Any:
    """Build the welcome card as a Rich renderable (for embedding in full-screen TUI)."""
    logo = Text(NAVI_LOGO.strip("\n"), style="bold blue")
    heading = Text.assemble((f"Welcome to {APP_NAME}!", "bold blue"))
    hint = Text("Run /help to get started.", style="grey50")

    head = Table(show_header=False, show_edge=False, box=None, padding=(0, 1), expand=False)
    head.add_column(justify="left")
    head.add_column(justify="left")
    head.add_row(logo, Group(heading, hint))

    rows = [
        head,
        Text(""),
        Text.assemble(("Directory: ", "bold grey50"), (str(workspace), "")),
        Text.assemble(("Session:   ", "bold grey50"), (session_id or "--", "")),
        Text.assemble(("Model:     ", "bold grey50"), (model, ""), ("  "), ("approval: ", "grey50"), (approval_mode, "yellow")),
        Text.assemble(("Version:   ", "bold grey50"), (VERSION, "")),
    ]

    return Panel(
        Group(*rows),
        border_style="blue",
        padding=(1, 2),
        expand=True,
    )


def print_splash(
    workspace: Path,
    model: str,
    approval_mode: str,
    session_id: str | None = None,
) -> None:
    """Print the welcome card to stdout (for non-full-screen modes like `navi run`)."""
    renderable = _build_welcome_renderable(workspace, model, approval_mode, session_id)
    console.print(renderable)


def print_chat_help() -> None:
    """
    chat 模式里的帮助信息。
    """
    help_text = "\n".join(
        [
            "[bold]Commands[/bold]",
            "",
            "[cyan]/help[/cyan]      Show this help",
            "[cyan]/clear[/cyan]     Clear screen",
            "[cyan]/tools[/cyan]     Show available tools",
            "[cyan]/skills[/cyan]    Show available skills",
            "[cyan]/sessions[/cyan]  Show recent sessions",
            "[cyan]/approval[/cyan]  Show approval mode and session approvals",
            "[cyan]/exit[/cyan]      Exit Navi",
            "",
            "Type a natural language task to start.",
        ]
    )

    console.print(
        Panel(
            help_text,
            title="Navi",
            border_style="dim",
        )
    )


def print_assistant_message(content: str) -> None:
    """
    打印模型最终回复。
    """
    console.print()
    console.print(
        Panel(
            Markdown(content),
            title="Navi",
            border_style="green",
        )
    )
    console.print()


def print_error_message(error: str) -> None:
    """
    打印错误信息。
    """
    from rich.markup import escape
    console.print()
    console.print(
        Panel(
            escape(error),
            title="Error",
            border_style="red",
        )
    )
    console.print()


def print_status_bar(runtime: AgentRuntime) -> None:
    usage = runtime.last_usage
    model = runtime.router.model_name
    window = runtime.router.context_window

    def fmt(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.1f}K"
        return str(n)

    if usage:
        prompt_t = usage.get("prompt_tokens", 0)
        comp_t = usage.get("completion_tokens", 0)
        usage_ratio = (prompt_t / window) if window else 0
        console.print(
            f"[dim]{format_context_status(usage_ratio, prompt_t, window)} "
            f"| out: {fmt(comp_t)} | model: {model}[/dim]"
        )
    else:
        console.print(f"[dim]model: {model}[/dim]")


def render_bottom_toolbar(runtime: AgentRuntime, workspace: Path, timer: dict | None = None):
    usage = runtime.last_usage
    model = runtime.router.model_name
    window = runtime.router.context_window

    prompt_t = usage.get("prompt_tokens", 0) if usage else 0
    comp_t = usage.get("completion_tokens", 0) if usage else 0
    pct = round(prompt_t / window * 100) if window else 0

    def fmt(n: int) -> str:
        if n >= 1_000_000:
            return f"{n / 1_000_000:.1f}M"
        if n >= 1000:
            return f"{n / 1000:.1f}K"
        return str(n)

    elapsed_str = _format_elapsed(timer) if timer else ""

    parts = [
        f"Context: {pct}%",
        f"In: {fmt(prompt_t)} / {fmt(window)}",
        f"Out: {fmt(comp_t)}",
        f"Model: {model}",
    ]
    if elapsed_str:
        parts.append(elapsed_str)
    return [("class:bottom-toolbar", " | ".join(parts))]


def _format_elapsed(timer: dict) -> str:
    """Format elapsed time for the status bar."""
    import time as _time
    start = timer.get("start")
    frozen = timer.get("frozen", 0.0)
    if start is not None:
        elapsed = max(0.0, _time.time() - start)
        emoji = "⏱"
    elif frozen > 0:
        elapsed = frozen
        emoji = "⏲"
    else:
        return ""

    minutes = int(elapsed // 60)
    seconds = int(elapsed % 60)
    if minutes > 0:
        return f"{emoji} {minutes}m {seconds}s"
    return f"{emoji} {seconds}s"



def print_agent_event(event: dict[str, Any], printer=None, box=None) -> None:
    _p = printer or console.print
    event_type = event.get("type")
    tool_name = event.get("tool_name")
    tool_args = event.get("tool_args") or {}
    tool_result = event.get("tool_result") or {}

    # ── Streaming events ──
    if event_type == "reasoning_delta":
        if box:
            box.reasoning_delta(event.get("content") or "")
        return

    if event_type == "assistant_delta":
        if box:
            box.response_delta(event.get("content") or "")
        return

    if event_type == "assistant_end":
        if box:
            box.close_all()
        return

    # ── Tool events (close boxes first) ──
    if box and box.has_output:
        box.close_all()

    if event_type == "tool_start":
        if tool_name == "run_command":
            command = tool_args.get("command", "")
            _p(f"[dim]┊ preparing {tool_name}[/dim] [bold]{command}[/bold]")
        elif tool_name in {"write_file", "patch_file"}:
            path = tool_args.get("path", "")
            _p(f"[dim]┊ preparing {tool_name}…[/dim] [bold]{path}[/bold]")
        elif tool_name == "read_file":
            path = tool_args.get("path", "")
            _p(f"[dim]┊ {tool_name}[/dim] [bold]{path}[/bold]")
        elif tool_name == "list_dir":
            path = tool_args.get("path", ".")
            _p(f"[dim]┊ {tool_name}[/dim] [bold]{path}[/bold]")
        elif tool_name == "skill_view":
            name = tool_args.get("name", "")
            _p(f"[dim]┊ {tool_name}[/dim] [bold]{name}[/bold]")
        else:
            _p(f"[dim]┊ {tool_name}[/dim]")

    elif event_type == "tool_result":
        elapsed = event.get("elapsed")
        elapsed_str = f"  {elapsed:.1f}s" if elapsed is not None else ""

        if tool_name in {"write_file", "patch_file"}:
            if not tool_result.get("ok"):
                _p(f"[red]┊ {tool_name} failed:[/red] {tool_result.get('error', 'Unknown error')}")
                return
            path = tool_result.get("path") or tool_args.get("path", "")
            added = tool_result.get("added_lines", 0)
            removed = tool_result.get("removed_lines", 0)
            _p(
                f"[dim]┊ {tool_name}[/dim] [bold]{path}[/bold] "
                f"[green]+{added}[/green] [red]-{removed}[/red]{elapsed_str}"
            )
            diff = tool_result.get("diff")
            if diff:
                diff_lines = diff.splitlines()
                if len(diff_lines) > 80:
                    diff = "\n".join(diff_lines[:80])
                    _p(Syntax(diff, "diff", word_wrap=True))
                    _p(f"[yellow]┊ ... ({len(diff_lines) - 80} more lines, showing first 80)[/yellow]")
                else:
                    _p(Syntax(diff, "diff", word_wrap=True))
            if tool_result.get("diff_truncated"):
                _p("[yellow]┊ diff truncated[/yellow]")

        elif tool_name == "run_command":
            exit_code = tool_result.get("exit_code")
            output = tool_result.get("output") or ""
            if tool_result.get("ok") or exit_code == 0:
                _p(f"[green]┊ exit_code=0[/green]{elapsed_str}")
            else:
                _p(f"[red]┊ exit_code={exit_code}[/red]{elapsed_str}")
            if output.strip():
                _p(Syntax(output[-4000:], "text", word_wrap=True))

        else:
            if tool_result.get("ok") is False:
                _p(f"[red]┊ {tool_name} failed:[/red] {tool_result.get('error', 'Unknown error')}")
            else:
                _p(f"[dim]┊ {tool_name}[/dim]{elapsed_str}")

    elif event_type == "tool_error":
        _p(f"[red]┊ {tool_name} error:[/red] {event.get('error')}")

    elif event_type == "assistant_content":
        pass


def ask_approval_from_cli(decision: ApprovalDecision) -> UserApprovalChoice:
    """
    CLI 审批交互。

    ApprovalManager 负责判断是否需要审批；
    这个函数只负责展示给用户并读取选择。
    支持方向键上下移动光标 + 回车选择，或直接按数字键 1/2/3。
    """
    import platform
    import sys

    # 打印原因
    console.print()
    console.print(approval_panel(decision))

    # 打印详情
    lines = [
        "[dim]Use ↑/↓ or 1/2/3, then press Enter.[/dim]",
    ]

    if decision.approval_key:
        lines.append(f"[dim]Approval key: {decision.approval_key}[/dim]")

    console.print("\n".join(lines))
    console.print()

    # 菜单选项，下面的代码实现的是用户审批的交互
    options = [
        ("1", "Allow once", UserApprovalChoice.ALLOW_ONCE),
        ("2", "Allow for this session", UserApprovalChoice.ALLOW_SESSION),
        ("3", "Reject", UserApprovalChoice.REJECT),
    ]
    selected = 0

    def render():
        for i, (num, label, _) in enumerate(options):
            prefix = "❯ " if i == selected else "  "
            print(f"{prefix}[{num}] {label}")

    render()

    if platform.system() == "Windows":
        import msvcrt

        while True:
            ch = msvcrt.getch()
            if ch == b'\xe0':          # 方向键前缀（Windows）
                ch2 = msvcrt.getch()
                if ch2 == b'H':        # 上
                    selected = (selected - 1) % 3
                elif ch2 == b'P':      # 下
                    selected = (selected + 1) % 3
            elif ch == b'\r':          # 回车
                return options[selected][2]
            elif ch == b'1':
                return options[0][2]
            elif ch == b'2':
                return options[1][2]
            elif ch == b'3':
                return options[2][2]
            else:
                continue

            # 重绘：光标上移 3 行，清除下方内容
            sys.stdout.write('\033[3A\033[J')
            render()
            sys.stdout.flush()
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == '\x1b':          # 方向键前缀（Unix）
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A':    # 上
                            selected = (selected - 1) % 3
                        elif ch3 == 'B':  # 下
                            selected = (selected + 1) % 3
                elif ch in ('\r', '\n'):  # 回车
                    return options[selected][2]
                elif ch == '1':
                    return options[0][2]
                elif ch == '2':
                    return options[1][2]
                elif ch == '3':
                    return options[2][2]
                else:
                    continue

                # 重绘：光标上移 3 行，清除下方内容
                sys.stdout.write('\033[3A\033[J')
                render()
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def resolve_approval_mode(
    approval: str,
    yolo: bool,
) -> str:
    """
    解析 CLI 审批参数。

    --yolo 等价于 --approval open。
    """
    if yolo:
        return "open"

    approval = approval.strip().lower()

    if approval not in APPROVAL_MODES:
        allowed = ", ".join(APPROVAL_MODES)
        raise typer.BadParameter(f"approval must be one of: {allowed}")

    return approval


# =========================
# Runtime result helpers
# =========================

def result_is_ok(result: Any) -> bool:
    """
    兼容两种返回值：
    1. AgentRunResult 对象：result.ok
    2. dict：result["ok"]
    """
    if isinstance(result, dict):
        return bool(result.get("ok"))

    return bool(getattr(result, "ok", False))


def result_final_answer(result: Any) -> str:
    """
    获取最终回答。
    """
    if isinstance(result, dict):
        return str(result.get("final_answer") or result.get("content") or "")

    return str(getattr(result, "final_answer", "") or "")


def result_error(result: Any) -> str:
    """
    获取错误信息。
    """
    if isinstance(result, dict):
        return str(result.get("error") or "Unknown error")

    return str(getattr(result, "error", None) or "Unknown error")


# =========================
# Slash commands
# =========================

def list_runtime_tools(runtime: AgentRuntime) -> list[str]:
    """
    优先从 runtime 里读取工具列表。
    如果 runtime 还没实现 list_tools，就返回你当前项目里的默认工具名。
    """
    if hasattr(runtime, "list_tools"):
        tools = runtime.list_tools()
        return [str(item) for item in tools]

    return [
        "list_dir",
        "read_file",
        "write_file",
        "patch_file",
        "run_command",
    ]


def list_skills_from_navi_home() -> list[str]:
    """
    从 Navi home 的 skills 目录扫描技能。
    第一版只扫描目录名和 SKILL.md。
    """
    skills_dir = get_navi_home() / "skills"

    if not skills_dir.exists():
        return []

    skills: list[str] = []

    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir():
            continue

        skill_md = item / "SKILL.md"
        if skill_md.exists():
            skills.append(item.name)

    return skills


def list_sessions_from_navi_home(limit: int = 20) -> list[dict]:
    """
    从 Navi home 的 sessions 目录读取历史 session 元数据。
    """
    import json as _json

    sessions_dir = get_navi_home() / "sessions"
    sessions: list[dict] = []

    if sessions_dir.exists():
        for item in sorted(sessions_dir.iterdir(), reverse=True):
            if not item.is_dir():
                continue
            meta_path = item / "meta.json"
            if not meta_path.is_file():
                continue
            try:
                meta = _json.loads(meta_path.read_text(encoding="utf-8"))
                sessions.append({
                    "session_id": meta.get("session_id", item.name),
                    "title": meta.get("title", "Untitled"),
                    "project_path": meta.get("project_path", ""),
                    "created_at": meta.get("created_at", ""),
                })
            except Exception:
                continue

    return sessions[:limit]


def print_sessions_table(limit: int = 5, current_session_id: str | None = None) -> None:
    sessions = list_sessions_from_navi_home(limit)

    if not sessions:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    # 把当前会话移到最前面并加标记
    if current_session_id:
        current_idx = None
        for i, s in enumerate(sessions):
            if s["session_id"] == current_session_id:
                current_idx = i
                break
        if current_idx is not None:
            current_session = sessions.pop(current_idx)
            sessions.insert(0, current_session)

    table = Table(show_header=True, header_style="bold")
    table.add_column("title", max_width=40)
    table.add_column("project_path", max_width=50)
    table.add_column("time", width=12)
    table.add_column("session_id", width=25)

    for s in sessions:
        created = s["created_at"]
        if len(created) >= 16:
            try:
                from datetime import datetime as _dt
                created = _dt.fromisoformat(created).strftime("%m-%d %H:%M")
            except Exception:
                created = created[:16]
        title = s["title"]
        # 当前会话加标记
        if current_session_id and s["session_id"] == current_session_id:
            title = f"[bold green]{title} (current)[/bold green]"
        table.add_row(title, s["project_path"], created, s["session_id"])

    console.print(table)


def _interactive_select(options: list[str], selected: int = 0) -> int | None:
    """方向键选择菜单，返回选中索引，Esc 返回 None。"""
    import platform
    import sys

    def render():
        for i, label in enumerate(options):
            prefix = "❯ " if i == selected else "  "
            print(f"{prefix}{label}")

    render()

    if platform.system() == "Windows":
        import msvcrt

        while True:
            ch = msvcrt.getch()
            if ch == b'\xe0':
                ch2 = msvcrt.getch()
                if ch2 == b'H':
                    selected = (selected - 1) % len(options)
                elif ch2 == b'P':
                    selected = (selected + 1) % len(options)
            elif ch == b'\r':
                return selected
            elif ch == b'\x1b':
                return None
            elif ch in [str(i + 1).encode() for i in range(len(options))]:
                return int(ch.decode()) - 1
            else:
                continue
            sys.stdout.write(f'\033[{len(options)}A\033[J')
            render()
            sys.stdout.flush()
    else:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch == '\x1b':
                    ch2 = sys.stdin.read(1)
                    if ch2 == '[':
                        ch3 = sys.stdin.read(1)
                        if ch3 == 'A':
                            selected = (selected - 1) % len(options)
                        elif ch3 == 'B':
                            selected = (selected + 1) % len(options)
                elif ch in ('\r', '\n'):
                    return selected
                elif ch == '\x1b':
                    return None
                elif ch.isdigit() and 1 <= int(ch) <= len(options):
                    return int(ch) - 1
                else:
                    continue
                sys.stdout.write(f'\033[{len(options)}A\033[J')
                render()
                sys.stdout.flush()
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def handle_model_command(runtime: AgentRuntime) -> None:
    info = runtime.get_model_info()
    providers = info["providers"]

    if not providers:
        console.print("[yellow]No providers configured. Edit ~/.navi/config.json[/yellow]")
        return

    # --- 第一层：选择供应商 ---
    current_provider = info["current_provider"]
    provider_labels = [f"{p}  [dim](current: {info['current_model']})[/dim]" if p == current_provider else p
                       for p in providers]

    selected_provider_idx = _interactive_select(provider_labels, providers.index(current_provider))
    if selected_provider_idx is None:
        return
    provider_name = providers[selected_provider_idx]

    # --- 第二层：选择模型 ---
    models = runtime.router.list_models(provider_name)
    model_names = list(models.keys())
    current_model = info["current_model"]

    current_model_idx = model_names.index(current_model) if current_model in model_names else 0
    model_labels = [f"{m} ◄" if m == current_model else m for m in model_names]

    selected_model_idx = _interactive_select(model_labels, current_model_idx)
    if selected_model_idx is None:
        return
    model_name = model_names[selected_model_idx]

    if runtime.switch_model(provider_name, model_name):
        console.print(f"[green]Switched to {provider_name} / {model_name}[/green]")
    else:
        console.print(f"[red]Failed to switch to {provider_name} / {model_name}[/red]")


def create_prompt_key_bindings(runtime: AgentRuntime | None = None) -> KeyBindings:
    """
    Make Tab accept history auto-suggestions.

    prompt_toolkit shows suggestions from history by default, but accepts them
    with Right/Ctrl-F rather than Tab unless an explicit binding is provided.
    """
    key_bindings = KeyBindings()

    @Condition
    def suggestion_available() -> bool:
        app = get_app()
        return (
            app.current_buffer.suggestion is not None
            and bool(app.current_buffer.suggestion.text)
            and app.current_buffer.document.is_cursor_at_the_end
        )

    @key_bindings.add("tab", filter=suggestion_available)
    def accept_suggestion(event) -> None:
        suggestion = event.current_buffer.suggestion
        if suggestion:
            event.current_buffer.insert_text(suggestion.text)

    @key_bindings.add("c-o")
    def show_more_sessions(event) -> None:
        current_session_id = runtime.session_store.session_id if runtime else None
        run_in_terminal(lambda: print_sessions_table(limit=20, current_session_id=current_session_id))

    return key_bindings

# / 开头命令
def handle_slash_command(
    command: str,
    runtime: AgentRuntime,
    workspace: Path,
) -> bool:
    """
    处理 chat 模式里的内部命令。

    返回 True 表示已经处理，不需要交给模型。
    返回 False 表示不是内部命令，应该交给 AgentRuntime。
    """
    if not command.startswith("/"):
        return False

    if command in {"/exit", "/quit"}:
        raise EOFError

    if command == "/help":
        print_chat_help()
        return True

    if command == "/clear":
        console.clear()
        return True

    if command == "/tools":
        tools = list_runtime_tools(runtime)

        if not tools:
            console.print("[yellow]No tools found.[/yellow]")
            return True

        console.print("[bold]Available tools[/bold]")
        for tool in tools:
            console.print(f"- {tool}")

        return True

    if command == "/skills":
        skills = list_skills_from_navi_home()

        if not skills:
            console.print("[yellow]No skills found.[/yellow]")
            return True

        console.print("[bold]Available skills[/bold]")
        for skill in skills:
            console.print(f"- {skill}")

        return True

    if command == "/sessions":
        current_session_id = runtime.session_store.session_id if runtime else None
        print_sessions_table(limit=5, current_session_id=current_session_id)
        console.print("[dim]Press Ctrl+O to show more sessions.[/dim]")
        return True

    if command == "/model":
        # 由 process_message 处理（需要 prompt_session 交互）
        return False

    if command == "/approval":
        mode = getattr(runtime.approval_manager, "mode", None)
        console.print(f"[bold]Approval mode[/bold]: {mode.value if mode else 'unknown'}")

        allowlist = getattr(runtime.approval_manager, "session_allowlist", set())
        if allowlist:
            console.print("[bold]Session approvals[/bold]")
            for item in sorted(allowlist):
                console.print(f"- {item}")
        else:
            console.print("[dim]No session approvals yet.[/dim]")

        return True

    console.print(f"[yellow]Unknown command:[/yellow] {command}")
    console.print("Type [cyan]/help[/cyan] to see available commands.")
    return True


# =========================
# Chat mode
# =========================

def start_chat(
    workspace: Path,
    max_steps: int,
    no_splash: bool,
    approval_mode: str,
    resume_session_id: str | None = None,
) -> None:
    """
    默认交互模式。

    navi
    navi chat

    都会进入这里。
    """
    asyncio.run(
        _start_chat_async(
            workspace=workspace,
            max_steps=max_steps,
            no_splash=no_splash,
            approval_mode=approval_mode,
            resume_session_id=resume_session_id,
        )
    )


def _print_live(*args, **kwargs) -> None:
    """Print through patch_stdout using prompt_toolkit's ANSI renderer.

    Rich Console objects cache their file handle at creation time, so a
    module-level Console still points at the pre-patch_stdout stdout.
    prompt_toolkit's print_formatted_text + ANSI goes through the current
    sys.stdout proxy and renders correctly above the input area.
    """
    from prompt_toolkit import print_formatted_text as _pt_print
    from prompt_toolkit.formatted_text import ANSI as _PT_ANSI

    # If called with a single Rich renderable (Markdown, Panel, etc.),
    # convert to ANSI string first.
    if len(args) == 1 and not isinstance(args[0], str):
        import io
        buf = io.StringIO()
        tmp = Console(file=buf, force_terminal=True,
                      color_system=console.color_system, highlight=False)
        tmp.print(*args, **kwargs)
        _pt_print(_PT_ANSI(buf.getvalue()))
        return

    # Plain string with Rich markup → strip markup for ANSI printing
    text = str(args[0]) if args else ""
    if "[" in text and "]" in text:
        # Rich markup: render to ANSI via temporary Console
        import io
        buf = io.StringIO()
        tmp = Console(file=buf, force_terminal=True,
                      color_system=console.color_system, highlight=False)
        tmp.print(*args, **kwargs)
        _pt_print(_PT_ANSI(buf.getvalue()))
    else:
        _pt_print(_PT_ANSI(text))


async def _start_chat_async(
    workspace: Path,
    max_steps: int,
    no_splash: bool,
    approval_mode: str,
    resume_session_id: str | None = None,
) -> None:
    load_navi_dotenv()

    workspace = workspace.resolve()

    stream_box = StreamingBox(_print_live)

    runtime = AgentRuntime(
        workspace=workspace,
        max_steps=max_steps,
        event_handler=lambda e: print_agent_event(e, box=stream_box),
        approval_mode=approval_mode,
        approval_handler=ask_approval_from_cli,
        resume_session_id=resume_session_id,
        on_output=_print_live,
    )

    navi_home = get_navi_home()

    def on_cancel_handler():
        runtime.interrupt()
        _print_live("\n⚡ Interrupting agent... (press Ctrl+C again to force exit)")

    prompt_session = NaviPromptSession(
        history_path=navi_home / "chat_history.txt",
        completer=WordCompleter(SLASH_COMMANDS, ignore_case=True),
        key_bindings=create_prompt_key_bindings(runtime),
        bottom_toolbar=lambda: render_bottom_toolbar(runtime, workspace, timer),
        on_cancel=on_cancel_handler,
    )

    # 计时器：记录用户发消息到收到回复的耗时
    timer: dict = {"start": None, "frozen": 0.0}

    # 欢迎信息直接打印到终端（Hermes 风格）
    if not no_splash and not resume_session_id:
        print_splash(workspace, runtime.router.model_name, approval_mode, runtime.session_store.session_id)

    async def process_message(text: str) -> None:
        """处理一条用户消息：斜杠命令或 Agent 对话轮次。"""
        # /model 命令：打开 model picker（需要 prompt_session 交互）
        if text.strip() == "/model":
            info = runtime.get_model_info()
            providers = info["providers"]
            if not providers:
                _print_live("[yellow]No providers configured. Edit ~/.navi/config.json[/yellow]")
                return

            def on_provider_selected(provider: str) -> list[str]:
                models = runtime.router.list_models(provider)
                return list(models.keys())

            def on_model_selected(provider: str, model: str) -> None:
                if runtime.switch_model(provider, model):
                    _print_live(f"[green]Switched to {provider} / {model}[/green]")
                else:
                    _print_live(f"[red]Failed to switch to {provider} / {model}[/red]")

            prompt_session.open_model_picker(
                providers=providers,
                current_provider=info["current_provider"],
                current_model=info["current_model"],
                on_provider_selected=on_provider_selected,
                on_model_selected=on_model_selected,
            )
            return

        # 其他斜杠命令
        if handle_slash_command(
            command=text,
            runtime=runtime,
            workspace=workspace,
        ):
            return

        # 待处理通知
        if runtime.reviewer.pending_message:
            msg = runtime.reviewer.pending_message
            runtime.reviewer.pending_message = None
            _print_live(f"[dim]💾 {msg}[/dim]")

        # 用户消息直接打印（Hermes 风格）
        _print_live()
        _print_live(Text(f"> {text}", style="#87CEEB"))
        _print_live()

        # 开始计时
        import time as _time
        timer["start"] = _time.time()
        timer["frozen"] = 0.0
        prompt_session.invalidate()

        prompt_session.begin_running()
        stream_box.reset()
        prompt_session._force_exit = False  # Reset force exit flag (use private attr for setter)

        # 后台定期刷新状态栏（让计时器实时更新）
        async def _tick_toolbar():
            while timer["start"] is not None:
                prompt_session.invalidate()
                await asyncio.sleep(1)

        tick_task = asyncio.ensure_future(_tick_toolbar())

        def runner():
            try:
                return runtime.run_turn(text)
            except KeyboardInterrupt:
                return {
                    "ok": False,
                    "error": "用户中断",
                    "final_answer": "",
                    "content": "",
                }

        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, runner)

        # 冻结计时器
        if timer["start"] is not None:
            timer["frozen"] = max(0.0, _time.time() - timer["start"])
            timer["start"] = None
        tick_task.cancel()
        prompt_session.invalidate()

        # 确保流式输出的 box 都关闭
        stream_box.close_all()
        prompt_session.end_running()

        # Double Ctrl+C → force exit
        if prompt_session.force_exit:
            prompt_session._app.exit(result="exit")
            raise EOFError("Force exit (double Ctrl+C)")

        # 如果没有任何流式输出（模型没产生 reasoning 或 content），打印 final_answer
        if not stream_box.had_output:
            answer = result.get("final_answer") or result.get("content") or ""
            if answer:
                _print_live()
                _print_live(Markdown(answer))

        if prompt_session.cancel_requested:
            _print_live("[yellow]Interrupted.[/yellow]")

        if not result_is_ok(result):
            _print_live(f"[red]{result_error(result)}[/red]")

        # 处理排队消息
        queued = prompt_session.take_queued()
        if queued:
            for msg in queued:
                await process_message(msg)

    await prompt_session.run_session(on_submit=process_message)

    sid = runtime.session_store.session_id
    console.print(f"[dim]To resume this session: navi --resume {sid}[/dim]")


# =========================
# Typer commands
# =========================

@app.callback(invoke_without_command=True)
def main_callback(
    ctx: typer.Context,
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory.",
        ),
    ] = Path("."),
    max_steps: Annotated[
        int,
        typer.Option(
            "--max-steps",
            help="Max agent loop steps.",
        ),
    ] = DEFAULT_MAX_STEPS,
    no_splash: Annotated[
        bool,
        typer.Option(
            "--no-splash",
            help="Skip splash screen.",
        ),
    ] = False,
    approval: Annotated[
        str,
        typer.Option(
            "--approval",
            help="Approval mode: strict, normal, or open.",
        ),
    ] = DEFAULT_APPROVAL_MODE,
    yolo: Annotated[
        bool,
        typer.Option(
            "--yolo",
            help="Alias for --approval open.",
        ),
    ] = False,
    resume: Annotated[
        str,
        typer.Option(
            "--resume",
            "-r",
            help="Resume a previous session by ID.",
        ),
    ] = "",
    continue_: Annotated[
        bool,
        typer.Option(
            "--continue",
            "-c",
            help="Resume the most recent session.",
        ),
    ] = False,
):
    """
    无子命令时，默认进入 chat 模式。
    """
    if resume and continue_:
        raise typer.BadParameter("--resume and --continue are mutually exclusive.")

    approval_mode = resolve_approval_mode(approval, yolo)

    resume_session_id = resume
    if continue_:
        sessions_dir = get_navi_home() / "sessions"
        if sessions_dir.exists():
            dirs = sorted(
                [d for d in sessions_dir.iterdir() if d.is_dir()],
                reverse=True,
            )
            if dirs:
                resume_session_id = dirs[0].name

    ctx.obj = {
        "workspace": workspace,
        "max_steps": max_steps,
        "no_splash": no_splash,
        "approval_mode": approval_mode,
        "resume_session_id": resume_session_id or None,
    }

    if ctx.invoked_subcommand is None:
        start_chat(
            workspace=workspace,
            max_steps=max_steps,
            no_splash=no_splash,
            approval_mode=approval_mode,
            resume_session_id=resume_session_id or None,
        )


@app.command()
def chat(ctx: typer.Context):
    """
    Enter interactive chat mode.
    """
    config = ctx.obj or {}

    start_chat(
        workspace=config.get("workspace", Path(".")),
        max_steps=config.get("max_steps", DEFAULT_MAX_STEPS),
        no_splash=config.get("no_splash", False),
        approval_mode=config.get("approval_mode", DEFAULT_APPROVAL_MODE),
        resume_session_id=config.get("resume_session_id"),
    )


@app.command()
def run(
    ctx: typer.Context,
    task: Annotated[
        str,
        typer.Argument(
            help="Natural language task to run.",
        ),
    ],
):
    """
    Run one task and exit.
    """
    load_navi_dotenv()

    config = ctx.obj or {}

    workspace = Path(config.get("workspace", Path("."))).resolve()
    max_steps = config.get("max_steps", DEFAULT_MAX_STEPS)
    approval_mode = config.get("approval_mode", DEFAULT_APPROVAL_MODE)

    resume_session_id = config.get("resume_session_id")

    runtime = AgentRuntime(
        workspace=workspace,
        max_steps=max_steps,
        event_handler=print_agent_event,
        approval_mode=approval_mode,
        approval_handler=ask_approval_from_cli,
        resume_session_id=resume_session_id,
        on_output=console.print,
    )

    console.print(
        Panel(
            f"[bold]Task[/bold]: {task}\n"
            f"[bold]Workspace[/bold]: {workspace}\n"
            f"[bold]Model[/bold]: {runtime.router.model_name}\n"
            f"[bold]Approval[/bold]: {approval_mode}",
            title="Navi",
            border_style="dim",
        )
    )

    result = run_with_stream_view(runtime, lambda: runtime.run_task(task))

    if result_is_ok(result):
        raise typer.Exit(code=0)

    print_error_message(result_error(result))
    raise typer.Exit(code=1)


@app.command()
def tools(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory.",
        ),
    ] = Path("."),
):
    """
    List available tools.
    """
    runtime = AgentRuntime(
        workspace=workspace.resolve(),
        max_steps=DEFAULT_MAX_STEPS,
    )

    tools_list = list_runtime_tools(runtime)

    if not tools_list:
        console.print("[yellow]No tools found.[/yellow]")
        return

    console.print("[bold]Available tools[/bold]")

    for tool in tools_list:
        console.print(f"- {tool}")


@app.command()
def skills(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory.",
        ),
    ] = Path("."),
):
    """
    List available skills.
    """
    skills_list = list_skills_from_navi_home()

    if not skills_list:
        console.print("[yellow]No skills found.[/yellow]")
        return

    console.print("[bold]Available skills[/bold]")

    for skill in skills_list:
        console.print(f"- {skill}")


@app.command()
def sessions(
    workspace: Annotated[
        Path,
        typer.Option(
            "--workspace",
            "-w",
            help="Workspace directory.",
        ),
    ] = Path("."),
):
    """
    List recent sessions.
    """
    print_sessions_table(limit=20)


def main():
    app()


if __name__ == "__main__":
    main()
