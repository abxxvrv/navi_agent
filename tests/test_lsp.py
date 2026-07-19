import json
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from navi_agent.integrations.lsp import LspManager
from navi_agent.runtime.interrupt_scope import TurnScope
from navi_agent.runtime.tool_context import CURRENT_TOOL_CONTEXT, ToolExecutionContext


def test_lsp_stdio_queries_document_sync_cancellation_and_shutdown(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "home"
    workspace.mkdir()
    navi_home.mkdir()
    source = workspace / "src" / "sample.py"
    source.parent.mkdir()
    source.write_text("class Example:\n    pass\n", encoding="utf-8")
    log_path = tmp_path / "lsp.jsonl"
    server_path = tmp_path / "server.py"
    server_path.write_text(
        r'''
import json
import os
import sys


def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in {b"\r\n", b"\n"}:
            break
        name, _, value = line.decode("ascii").partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    return json.loads(sys.stdin.buffer.read(length))


def send(message):
    body = json.dumps(message, separators=(",", ":")).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode())
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def record(message):
    with open(os.environ["LSP_LOG"], "a", encoding="utf-8") as stream:
        stream.write(json.dumps(message, separators=(",", ":")) + "\n")


message = read_message()
record(message)
send({
    "jsonrpc": "2.0",
    "id": 900,
    "method": "workspace/configuration",
    "params": {"items": [{"section": "python"}, {"section": "lint"}]},
})
record(read_message())
send({"jsonrpc": "2.0", "id": 901, "method": "custom/unknown", "params": {}})
record(read_message())
send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})

target_uri = os.environ["TARGET_URI"]
while True:
    message = read_message()
    if message is None:
        break
    record(message)
    method = message.get("method")
    if method == "exit":
        break
    if "id" not in message:
        continue
    if method == "textDocument/definition":
        result = [{
            "targetUri": target_uri,
            "targetSelectionRange": {
                "start": {"line": 2, "character": 3},
                "end": {"line": 2, "character": 10},
            },
        }]
    elif method == "textDocument/references":
        result = [{
            "uri": target_uri,
            "range": {
                "start": {"line": 4, "character": 5},
                "end": {"line": 4, "character": 8},
            },
        }]
    elif method == "textDocument/implementation":
        result = {
            "uri": target_uri,
            "range": {
                "start": {"line": 6, "character": 7},
                "end": {"line": 6, "character": 9},
            },
        }
    elif method == "textDocument/documentSymbol":
        result = [{
            "name": "Example",
            "kind": 5,
            "range": {"start": {"line": 0, "character": 0}},
            "selectionRange": {"start": {"line": 0, "character": 6}},
            "children": [{
                "name": "method",
                "kind": 6,
                "range": {"start": {"line": 1, "character": 4}},
                "selectionRange": {"start": {"line": 1, "character": 8}},
            }],
        }]
    elif method == "workspace/symbol" and message["params"]["query"] == "hang":
        continue
    elif method == "workspace/symbol":
        result = [{
            "name": "Example",
            "kind": 5,
            "containerName": "module",
            "location": {
                "uri": target_uri,
                "range": {"start": {"line": 8, "character": 1}},
            },
        }]
    elif method == "shutdown":
        result = None
    else:
        result = None
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
''',
        encoding="utf-8",
    )
    (navi_home / "lsp.json").write_text(
        json.dumps(
            {
                "python": {
                    "command": sys.executable,
                    "args": [str(server_path)],
                    "env": {
                        "LSP_LOG": str(log_path),
                        "TARGET_URI": source.resolve().as_uri(),
                    },
                    "extensions": {".py": "python"},
                    "settings": {"python": {"analysis": "strict"}},
                }
            }
        ),
        encoding="utf-8",
    )

    manager = LspManager(workspace, navi_home=navi_home)
    assert manager.query(
        "goToDefinition", "src/sample.py", line=0, character=6
    ) == {
        "ok": True,
        "operation": "goToDefinition",
        "count": 1,
        "truncated": False,
        "locations": [{"path": "src/sample.py", "line": 3, "character": 4}],
    }
    assert manager.query(
        "findReferences", "src/sample.py", line=0, character=6
    )["locations"] == [{"path": "src/sample.py", "line": 5, "character": 6}]
    assert manager.query(
        "goToImplementation", "src/sample.py", line=0, character=6
    )["locations"] == [{"path": "src/sample.py", "line": 7, "character": 8}]
    assert manager.query("documentSymbol", "src/sample.py")["symbols"] == [
        {
            "name": "Example",
            "kind": 5,
            "path": "src/sample.py",
            "line": 1,
            "character": 7,
        },
        {
            "name": "method",
            "kind": 6,
            "path": "src/sample.py",
            "line": 2,
            "character": 9,
            "container": "Example",
        },
    ]
    assert manager.query("workspaceSymbol", query="Exam")["symbols"] == [
        {
            "name": "Example",
            "kind": 5,
            "path": "src/sample.py",
            "line": 9,
            "character": 2,
            "container": "module",
        }
    ]

    scope = TurnScope()
    timer = threading.Timer(0.1, scope.cancel)
    token = CURRENT_TOOL_CONTEXT.set(ToolExecutionContext(scope, "lsp-call"))
    timer.start()
    try:
        with pytest.raises(KeyboardInterrupt):
            manager.query("workspaceSymbol", query="hang")
    finally:
        timer.join()
        CURRENT_TOOL_CONTEXT.reset(token)
        manager.close()

    messages = [json.loads(line) for line in log_path.read_text().splitlines()]
    methods = [message.get("method") for message in messages]
    assert methods.count("textDocument/didOpen") == 1
    assert methods.count("textDocument/didChange") == 3
    assert methods.count("textDocument/didSave") == 4
    assert "$/cancelRequest" in methods
    assert methods[-2:] == ["shutdown", "exit"]
    references = next(
        message for message in messages if message.get("method") == "textDocument/references"
    )
    assert references["params"]["context"] == {"includeDeclaration": True}
    assert next(message for message in messages if message.get("id") == 900)["result"] == [
        {"analysis": "strict"},
        None,
    ]
    assert next(message for message in messages if message.get("id") == 901)["error"][
        "code"
    ] == -32601


def test_lsp_config_precedence_routing_and_input_validation(tmp_path):
    navi_home = tmp_path / "home"
    navi_home.mkdir()
    (navi_home / "lsp.json").write_text(
        json.dumps(
            {
                "lspServers": {
                    "alpha": {
                        "command": "user-alpha",
                        "extensionToLanguage": {"py": "python"},
                    },
                    "beta": {
                        "command": "user-beta",
                        "extensions": {".py": "python"},
                    },
                    "invalid": {"command": ["not-a-string"]},
                }
            }
        ),
        encoding="utf-8",
    )
    manager = LspManager(
        tmp_path,
        navi_home=navi_home,
        plugin_servers={
            "alpha": {
                "command": "plugin-alpha",
                "extensions": {".py": "python"},
            },
            "plugin-only": {
                "command": "plugin-js",
                "extensions": {".js": "javascript"},
            },
        },
    )

    assert manager.servers["alpha"]["command"] == "user-alpha"
    assert "invalid" not in manager.servers
    assert manager._extensions[".py"] == ("alpha", "python")
    assert manager._extensions[".js"] == ("plugin-only", "javascript")
    assert manager.query("workspaceSymbol", query=" ")["ok"] is False
    assert manager.query("goToDefinition", "sample.py")["ok"] is False
    assert manager.clients == {}


def test_lsp_discards_dead_client_for_next_lazy_start(tmp_path):
    source = tmp_path / "sample.py"
    source.write_text("value = 1\n", encoding="utf-8")
    manager = LspManager(
        tmp_path,
        navi_home=tmp_path / "home",
        plugin_servers={
            "python": {
                "command": "unused",
                "extensions": {".py": "python"},
            }
        },
    )
    client = SimpleNamespace(
        process=SimpleNamespace(poll=lambda: 1),
        _reader_error="server closed stdout",
        sync_document=Mock(side_effect=RuntimeError("server stopped")),
        close=Mock(),
    )
    manager.clients["python"] = client

    result = manager.query("goToDefinition", "sample.py", line=0, character=0)

    assert result["ok"] is False
    assert manager.clients == {}
    client.close.assert_called_once_with()
