from io import StringIO
from pathlib import Path
from types import SimpleNamespace

from rich.console import Console

from navi_agent.cli import main
from navi_agent.integrations import mcp_commands
from navi_agent.storage import history_store
from navi_agent import paths


def _runtime_stub() -> SimpleNamespace:
    return SimpleNamespace(
        session_store=SimpleNamespace(session_id="session-1"),
        tool_registry=object(),
        approval_manager=SimpleNamespace(mode=None, session_allowlist=set()),
    )


def test_handle_slash_command_returns_false_for_non_slash_input() -> None:
    runtime = _runtime_stub()

    assert main.handle_slash_command("hello", runtime, Path(".")) is False
    assert main.handle_slash_command(" /help", runtime, Path(".")) is False


def test_handle_slash_command_returns_false_for_unknown_slash_inputs() -> None:
    runtime = _runtime_stub()

    for command in (
        "/api/v1/users",
        "/unknown",
        "/searching abc",
        "/mcpserver status",
        "/help me",
        "/compress this text",
        "/model foo",
    ):
        assert main.handle_slash_command(command, runtime, Path(".")) is False


def test_handle_slash_command_does_not_print_for_unknown_slash(monkeypatch) -> None:
    printed: list[object] = []

    monkeypatch.setattr(main.console, "print", lambda *args, **kwargs: printed.append((args, kwargs)))

    assert main.handle_slash_command("/unknown", _runtime_stub(), Path(".")) is False
    assert printed == []


def test_handle_slash_command_handles_help(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(main, "print_chat_help", lambda **kwargs: calls.append(kwargs["printer"]))

    printer = lambda *args, **kwargs: None
    assert main.handle_slash_command("/help", _runtime_stub(), Path("."), printer=printer) is True
    assert calls == [printer]


def test_loop_is_listed_in_completion_and_help() -> None:
    output = StringIO()
    console = Console(file=output, color_system=None, width=100)

    main.print_chat_help(printer=console.print)

    assert "/loop" in main.SLASH_COMMANDS
    assert "/loop" in output.getvalue()


def test_loop_with_arguments_reaches_the_agent() -> None:
    assert main.handle_slash_command(
        "/loop 5m check deployment",
        _runtime_stub(),
        Path("."),
    ) is False


def test_handle_slash_command_handles_search_with_tab_separator(monkeypatch, tmp_path) -> None:
    history_file = tmp_path / "history.sqlite3"
    history_file.write_text("", encoding="utf-8")

    queries: list[str] = []

    monkeypatch.setattr(paths, "get_navi_home", lambda: tmp_path)
    monkeypatch.setattr(history_store.HistoryStore, "_connect", lambda self: object())
    monkeypatch.setattr(
        history_store.HistoryStore,
        "search_messages",
        lambda self, query, limit=10: queries.append(query) or [],
    )

    assert main.handle_slash_command("/search\tabc", _runtime_stub(), Path(".")) is True
    assert queries == ["abc"]


def test_handle_slash_command_handles_mcp_with_tab_separator(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    monkeypatch.setattr(
        mcp_commands,
        "handle_mcp_command",
        lambda args, tool_registry: calls.append((args, tool_registry)) or "ok",
    )

    runtime = _runtime_stub()
    assert main.handle_slash_command("/mcp\tstatus", runtime, Path(".")) is True
    assert calls == [("status", runtime.tool_registry)]


def test_handle_slash_command_uses_custom_printer(monkeypatch) -> None:
    printed: list[object] = []

    monkeypatch.setattr(main, "list_runtime_tools", lambda runtime: ["read_file"])
    monkeypatch.setattr(
        main.console,
        "print",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("console.print used")),
    )

    assert main.handle_slash_command(
        "/tools",
        _runtime_stub(),
        Path("."),
        printer=lambda *args, **kwargs: printed.append((args, kwargs)),
    ) is True

    assert printed == [(("[bold]Available tools[/bold]",), {}), (("- read_file",), {})]
