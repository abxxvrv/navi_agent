from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer
from dotenv import load_dotenv
from prompt_toolkit import PromptSession
from prompt_toolkit.application.current import get_app
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.filters import Condition
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.syntax import Syntax
from rich.text import Text

from navi_agent.runtime import AgentRuntime


APP_NAME = "Navi"
VERSION = "0.1.0"

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_STEPS = 40
SLASH_COMMANDS = [
    "/help",
    "/clear",
    "/tools",
    "/skills",
    "/sessions",
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

console = Console()


# =========================
# UI
# =========================

def print_splash(
    workspace: Path,
    model: str,
    no_wait: bool = False,
) -> None:
    """
    启动欢迎页。
    类似 Claude Code / Codex CLI 那种进入终端产品前的展示页。
    """
    console.clear()

    welcome = Text()
    welcome.append("* ", style="bold red")
    welcome.append("Welcome to ", style="dim")
    welcome.append(APP_NAME, style="bold")
    welcome.append(" - your local project navigator.", style="dim")

    console.print(
        Panel(
            welcome,
            border_style="dim",
            padding=(0, 1),
        )
    )

    console.print(NAVI_LOGO, style="bold cyan")

    console.print()
    console.print(f"[dim]Workspace:[/dim] [bold]{workspace}[/bold]")
    console.print(f"[dim]Model:[/dim] [bold]{model}[/bold]")
    console.print(f"[dim]Version:[/dim] [bold]{VERSION}[/bold]")
    console.print()

    console.print(
        "[green]Ready.[/green] "
        "[dim]Press[/dim] [bold]Enter[/bold] [dim]to continue[/dim]"
    )

    if not no_wait:
        input()

    console.clear()


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
    console.print()
    console.print(
        Panel(
            error,
            title="Error",
            border_style="red",
        )
    )
    console.print()


def print_agent_event(event: dict[str, Any]) -> None:
    event_type = event.get("type")
    tool_name = event.get("tool_name")
    tool_args = event.get("tool_args") or {}
    tool_result = event.get("tool_result") or {}

    if event_type == "tool_start":
        if tool_name == "run_command":
            command = tool_args.get("command", "")
            console.print(f"\n[dim]• Ran[/dim] [bold]{command}[/bold]")
        elif tool_name in {"write_file", "patch_file"}:
            path = tool_args.get("path", "")
            console.print(f"\n[dim]• Editing[/dim] [bold]{path}[/bold]")
        elif tool_name == "read_file":
            path = tool_args.get("path", "")
            console.print(f"\n[dim]• Read[/dim] [bold]{path}[/bold]")
        elif tool_name == "list_dir":
            path = tool_args.get("path", ".")
            console.print(f"\n[dim]• Listed[/dim] [bold]{path}[/bold]")
        elif tool_name == "load_skill":
            name = tool_args.get("name", "")
            console.print(f"\n[dim]• Loading skill[/dim] [bold]{name}[/bold]")
        elif tool_name == "skill_view":
            name = tool_args.get("name", "")
            console.print(f"\n[dim]• Viewing skill[/dim] [bold]{name}[/bold]")
        else:
            console.print(f"\n[dim]• Tool[/dim] [bold]{tool_name}[/bold]")

    elif event_type == "tool_result":
        if tool_name in {"write_file", "patch_file"}:
            if not tool_result.get("ok"):
                console.print(
                    f"[red]  └ failed:[/red] "
                    f"{tool_result.get('error', 'Unknown error')}"
                )
                return

            path = tool_result.get("path") or tool_args.get("path", "")
            added = tool_result.get("added_lines", 0)
            removed = tool_result.get("removed_lines", 0)

            console.print(
                f"[dim]• Edited[/dim] [bold]{path}[/bold] "
                f"([green]+{added}[/green] [red]-{removed}[/red])"
            )

            diff = tool_result.get("diff")
            if diff:
                console.print(Syntax(diff, "diff", word_wrap=True))

            if tool_result.get("diff_truncated"):
                console.print("[yellow]  └ diff truncated[/yellow]")

        elif tool_name == "run_command":
            exit_code = tool_result.get("exit_code")
            output = tool_result.get("output") or ""

            if tool_result.get("ok") or exit_code == 0:
                console.print("[green]  └ exit_code=0[/green]")
            else:
                console.print(f"[red]  └ exit_code={exit_code}[/red]")

            if output.strip():
                console.print(Syntax(output[-4000:], "text", word_wrap=True))

        elif tool_name == "load_skill":
            if tool_result.get("ok"):
                skill_name = tool_result.get("skill_name") or tool_args.get("name")
                console.print(f"[green]  └ loaded {skill_name}[/green]")
            else:
                console.print(
                    f"[red]  └ failed:[/red] "
                    f"{tool_result.get('error', 'Unknown error')}"
                )

        else:
            if tool_result.get("ok") is False:
                console.print(
                    f"[red]  └ failed:[/red] "
                    f"{tool_result.get('error', 'Unknown error')}"
                )

    elif event_type == "tool_error":
        console.print(f"[red]• Tool error {tool_name}:[/red] {event.get('error')}")


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
        "load_skill",
    ]


def list_skills_from_workspace(workspace: Path) -> list[str]:
    """
    从源码目录的 skills 目录扫描技能。
    第一版只扫描目录名和 SKILL.md。
    """
    skills_dir = Path(__file__).resolve().parent / "skills"

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


def list_sessions_from_workspace(workspace: Path, limit: int = 20) -> list[str]:
    """
    从 .navi/sessions 或 .light_agent/sessions 里读取历史 session。
    兼容你之前的 .light_agent 目录。
    """
    candidates = [
        workspace / ".navi" / "sessions",
        workspace / ".light_agent" / "sessions",
    ]

    sessions: list[str] = []

    for sessions_dir in candidates:
        if not sessions_dir.exists():
            continue

        for item in sorted(sessions_dir.iterdir(), reverse=True):
            if item.is_dir():
                sessions.append(item.name)

    return sessions[:limit]


def create_prompt_key_bindings() -> KeyBindings:
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

    return key_bindings


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
        skills = list_skills_from_workspace(workspace)

        if not skills:
            console.print("[yellow]No skills found.[/yellow]")
            return True

        console.print("[bold]Available skills[/bold]")
        for skill in skills:
            console.print(f"- {skill}")

        return True

    if command == "/sessions":
        sessions = list_sessions_from_workspace(workspace)

        if not sessions:
            console.print("[yellow]No sessions found.[/yellow]")
            return True

        console.print("[bold]Recent sessions[/bold]")
        for session in sessions:
            console.print(f"- {session}")

        return True

    console.print(f"[yellow]Unknown command:[/yellow] {command}")
    console.print("Type [cyan]/help[/cyan] to see available commands.")
    return True


# =========================
# Chat mode
# =========================

def start_chat(
    workspace: Path,
    model: str,
    max_steps: int,
    no_splash: bool,
) -> None:
    """
    默认交互模式。

    navi
    navi chat

    都会进入这里。
    """
    load_dotenv()

    workspace = workspace.resolve()

    runtime = AgentRuntime(
        workspace=workspace,
        model=model,
        max_steps=max_steps,
        event_handler=print_agent_event,
    )

    if not no_splash:
        print_splash(
            workspace=workspace,
            model=model,
        )

    print_chat_help()

    navi_dir = workspace / ".navi"
    navi_dir.mkdir(parents=True, exist_ok=True)

    prompt_session = PromptSession(
        history=FileHistory(str(navi_dir / "chat_history.txt")),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(SLASH_COMMANDS, ignore_case=True),
        key_bindings=create_prompt_key_bindings(),
    )

    while True:
        try:
            user_input = prompt_session.prompt("You > ")
            text = user_input.strip()

            if not text:
                continue

            handled = handle_slash_command(
                command=text,
                runtime=runtime,
                workspace=workspace,
            )

            if handled:
                continue

            console.print("[dim]Thinking...[/dim]")
            result = runtime.run_turn(text)

            if result_is_ok(result):
                print_assistant_message(result_final_answer(result))
            else:
                print_error_message(result_error(result))

        except KeyboardInterrupt:
            console.print("\n[yellow]Interrupted.[/yellow]")
            continue

        except EOFError:
            console.print("\n[yellow]Bye.[/yellow]")
            break


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
    model: Annotated[
        str,
        typer.Option(
            "--model",
            "-m",
            help="Model name.",
        ),
    ] = DEFAULT_MODEL,
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
):
    """
    无子命令时，默认进入 chat 模式。
    """
    ctx.obj = {
        "workspace": workspace,
        "model": model,
        "max_steps": max_steps,
        "no_splash": no_splash,
    }

    if ctx.invoked_subcommand is None:
        start_chat(
            workspace=workspace,
            model=model,
            max_steps=max_steps,
            no_splash=no_splash,
        )


@app.command()
def chat(ctx: typer.Context):
    """
    Enter interactive chat mode.
    """
    config = ctx.obj or {}

    start_chat(
        workspace=config.get("workspace", Path(".")),
        model=config.get("model", DEFAULT_MODEL),
        max_steps=config.get("max_steps", DEFAULT_MAX_STEPS),
        no_splash=config.get("no_splash", False),
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
    load_dotenv()

    config = ctx.obj or {}

    workspace = Path(config.get("workspace", Path("."))).resolve()
    model = config.get("model", DEFAULT_MODEL)
    max_steps = config.get("max_steps", DEFAULT_MAX_STEPS)

    runtime = AgentRuntime(
        workspace=workspace,
        model=model,
        max_steps=max_steps,
        event_handler=print_agent_event,
    )

    console.print(
        Panel(
            f"[bold]Task[/bold]: {task}\n"
            f"[bold]Workspace[/bold]: {workspace}\n"
            f"[bold]Model[/bold]: {model}",
            title="Navi",
            border_style="dim",
        )
    )

    console.print("[dim]Thinking...[/dim]")
    result = runtime.run_task(task)

    if result_is_ok(result):
        print_assistant_message(result_final_answer(result))
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
    model: Annotated[
        str,
        typer.Option(
            "--model",
            "-m",
            help="Model name.",
        ),
    ] = DEFAULT_MODEL,
):
    """
    List available tools.
    """
    runtime = AgentRuntime(
        workspace=workspace.resolve(),
        model=model,
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
    workspace = workspace.resolve()
    skills_list = list_skills_from_workspace(workspace)

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
    workspace = workspace.resolve()
    sessions_list = list_sessions_from_workspace(workspace)

    if not sessions_list:
        console.print("[yellow]No sessions found.[/yellow]")
        return

    console.print("[bold]Recent sessions[/bold]")

    for session in sessions_list:
        console.print(f"- {session}")


def main():
    app()


if __name__ == "__main__":
    main()
