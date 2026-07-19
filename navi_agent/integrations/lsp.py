from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..runtime.tool_context import CURRENT_TOOL_CONTEXT

logger = logging.getLogger(__name__)


class LspClient:
    def __init__(
        self,
        name: str,
        config: dict[str, Any],
        workspace: Path,
    ) -> None:
        self.name = name
        self.config = config
        workspace_folder = config.get("workspaceFolder", config.get("workspace_folder"))
        self.workspace = (
            (workspace / workspace_folder).resolve()
            if isinstance(workspace_folder, str) and not Path(workspace_folder).is_absolute()
            else Path(workspace_folder).resolve()
            if isinstance(workspace_folder, str)
            else workspace
        )
        self.process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._documents: dict[str, int] = {}
        self._next_id = 1
        self._reader_error: str | None = None

    def initialize(self) -> None:
        if self.process is not None:
            return

        command = self.config["command"]
        args = self.config.get("args", [])
        env = os.environ.copy()
        env.update(self.config.get("env", {}))
        process_options: dict[str, Any] = {"start_new_session": True}
        if os.name == "nt":
            process_options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        self.process = subprocess.Popen(
            [command, *args],
            cwd=self.workspace,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            **process_options,
        )
        self._reader_error = None
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"navi-lsp-{self.name}",
            daemon=True,
        )
        self._reader.start()

        try:
            root_uri = self.workspace.as_uri()
            result = self.request(
                "initialize",
                {
                    "processId": os.getpid(),
                    "rootUri": root_uri,
                    "workspaceFolders": [
                        {"uri": root_uri, "name": self.workspace.name or "workspace"}
                    ],
                    "capabilities": {
                        "workspace": {
                            "configuration": True,
                            "symbol": {},
                            "workspaceFolders": True,
                        },
                        "textDocument": {
                            "synchronization": {"didSave": True},
                            "definition": {"linkSupport": True},
                            "references": {},
                            "implementation": {"linkSupport": True},
                            "documentSymbol": {
                                "hierarchicalDocumentSymbolSupport": True
                            },
                        },
                    },
                    "initializationOptions": self.config.get(
                        "initializationOptions",
                        self.config.get("initialization_options"),
                    ),
                },
                timeout=float(self.config.get("startupTimeout", 15_000)) / 1000,
            )
            if not isinstance(result, dict):
                raise RuntimeError(f"LSP server '{self.name}' returned an invalid initialize result")
            self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
            if self.config.get("settings") is not None:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "workspace/didChangeConfiguration",
                        "params": {"settings": self.config["settings"]},
                    }
                )
        except BaseException:
            process = self.process
            self.process = None
            if process is not None and process.poll() is None:
                if os.name == "nt":
                    process.kill()
                else:
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise

    def _send(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise RuntimeError(f"LSP server '{self.name}' is not running")
        body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        with self._write_lock:
            process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
            process.stdin.write(body)
            process.stdin.flush()

    def _read_loop(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            while True:
                content_length: int | None = None
                while True:
                    line = process.stdout.readline()
                    if not line:
                        raise EOFError("server closed stdout")
                    if line in {b"\r\n", b"\n"}:
                        break
                    name, _, value = line.decode("ascii", errors="replace").partition(":")
                    if name.lower() == "content-length":
                        content_length = int(value.strip())
                if content_length is None:
                    raise RuntimeError("missing Content-Length header")
                body = process.stdout.read(content_length)
                if len(body) != content_length:
                    raise EOFError("incomplete LSP message")
                message = json.loads(body.decode("utf-8"))
                if "method" in message and "id" in message:
                    method = message["method"]
                    params = message.get("params") or {}
                    if method == "workspace/configuration":
                        items = params.get("items", []) if isinstance(params, dict) else []
                        result: Any = []
                        for item in items:
                            value = self.config.get("settings")
                            section = item.get("section") if isinstance(item, dict) else None
                            if isinstance(section, str) and section:
                                for part in section.split("."):
                                    if not isinstance(value, dict) or part not in value:
                                        value = None
                                        break
                                    value = value[part]
                            result.append(value)
                    elif method == "workspace/workspaceFolders":
                        result = [
                            {
                                "uri": self.workspace.as_uri(),
                                "name": self.workspace.name or "workspace",
                            }
                        ]
                    elif method == "workspace/applyEdit":
                        result = {"applied": False}
                    else:
                        self._send(
                            {
                                "jsonrpc": "2.0",
                                "id": message["id"],
                                "error": {
                                    "code": -32601,
                                    "message": f"Method not found: {method}",
                                },
                            }
                        )
                        continue
                    self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
                elif "id" in message:
                    with self._state_lock:
                        pending = self._pending.get(message["id"])
                    if pending is not None:
                        pending[1]["message"] = message
                        pending[0].set()
        except Exception as exc:
            self._reader_error = str(exc)
            with self._state_lock:
                pending = list(self._pending.values())
            for event, _ in pending:
                event.set()

    def request(
        self,
        method: str,
        params: Any,
        timeout: float = 30.0,
    ) -> Any:
        with self._state_lock:
            request_id = self._next_id
            self._next_id += 1
            event = threading.Event()
            response: dict[str, Any] = {}
            self._pending[request_id] = (event, response)
        try:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": method,
                    "params": params,
                }
            )
            deadline = time.monotonic() + timeout
            while not event.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                context = CURRENT_TOOL_CONTEXT.get()
                if (
                    context is not None
                    and context.scope is not None
                    and context.scope.is_cancelled()
                ):
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "$/cancelRequest",
                            "params": {"id": request_id},
                        }
                    )
                    raise KeyboardInterrupt("用户中断")
                if time.monotonic() >= deadline:
                    self._send(
                        {
                            "jsonrpc": "2.0",
                            "method": "$/cancelRequest",
                            "params": {"id": request_id},
                        }
                    )
                    raise TimeoutError(f"LSP request '{method}' timed out after {timeout:g}s")
            message = response.get("message")
            if message is None:
                raise RuntimeError(
                    f"LSP server '{self.name}' stopped: {self._reader_error or 'unknown error'}"
                )
            if "error" in message:
                error = message["error"]
                raise RuntimeError(
                    error.get("message", str(error)) if isinstance(error, dict) else str(error)
                )
            return message.get("result")
        finally:
            with self._state_lock:
                self._pending.pop(request_id, None)

    def sync_document(self, path: Path, language_id: str) -> str:
        content = path.read_text(encoding="utf-8")
        uri = path.as_uri()
        with self._state_lock:
            version = self._documents.get(uri)
            self._documents[uri] = 0 if version is None else version + 1
            if version is None:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didOpen",
                        "params": {
                            "textDocument": {
                                "uri": uri,
                                "languageId": language_id,
                                "version": 0,
                                "text": content,
                            }
                        },
                    }
                )
            else:
                self._send(
                    {
                        "jsonrpc": "2.0",
                        "method": "textDocument/didChange",
                        "params": {
                            "textDocument": {"uri": uri, "version": version + 1},
                            "contentChanges": [{"text": content}],
                        },
                    }
                )
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "textDocument/didSave",
                    "params": {
                        "textDocument": {"uri": uri},
                        "text": content,
                    },
                }
            )
        return uri

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                try:
                    if self._reader_error is not None:
                        raise RuntimeError(self._reader_error)
                    self.request(
                        "shutdown",
                        None,
                        timeout=float(self.config.get("shutdownTimeout", 5_000)) / 1000,
                    )
                    self._send({"jsonrpc": "2.0", "method": "exit"})
                    process.wait(
                        timeout=float(self.config.get("shutdownTimeout", 5_000)) / 1000
                    )
                except (Exception, KeyboardInterrupt):
                    if os.name == "nt":
                        process.terminate()
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        if os.name == "nt":
                            process.kill()
                        else:
                            os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
        finally:
            self.process = None
            if self._reader is not None:
                self._reader.join(timeout=1)


class LspManager:
    def __init__(
        self,
        workspace: str | Path,
        plugin_servers: dict[str, Any] | None = None,
        navi_home: str | Path | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.navi_home = (
            Path(navi_home).resolve()
            if navi_home is not None
            else Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()
        )
        configured = dict(plugin_servers or {})
        user_path = self.navi_home / "lsp.json"
        if user_path.is_file():
            try:
                loaded = json.loads(user_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    user_servers = loaded.get("lspServers", loaded)
                    if isinstance(user_servers, dict):
                        configured.update(user_servers)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Invalid LSP configuration %s: %s", user_path, exc)

        self.servers: dict[str, dict[str, Any]] = {}
        self._extensions: dict[str, tuple[str, str]] = {}
        for name, value in sorted(configured.items()):
            if not isinstance(name, str) or not isinstance(value, dict):
                continue
            command = value.get("command")
            args = value.get("args", [])
            env = value.get("env", {})
            transport = value.get("transport", "stdio")
            extensions = value.get(
                "extensions",
                value.get("extensionToLanguage", value.get("extensionToLanguageId", {})),
            )
            if (
                not isinstance(command, str)
                or not command
                or not isinstance(args, list)
                or not all(isinstance(arg, str) for arg in args)
                or not isinstance(env, dict)
                or not all(isinstance(key, str) and isinstance(item, str) for key, item in env.items())
                or transport != "stdio"
                or not isinstance(extensions, dict)
            ):
                logger.warning("Skipping invalid LSP server configuration: %s", name)
                continue
            config = dict(value)
            config["args"] = args
            config["env"] = env
            config["extensions"] = extensions
            self.servers[name] = config
            for extension, language_id in extensions.items():
                if isinstance(extension, str) and isinstance(language_id, str):
                    normalized = extension.lower()
                    if not normalized.startswith("."):
                        normalized = f".{normalized}"
                    self._extensions.setdefault(normalized, (name, language_id))
        self.clients: dict[str, LspClient] = {}
        self._lock = threading.Lock()

    def query(
        self,
        operation: str,
        file_path: str | None = None,
        line: int | None = None,
        character: int | None = None,
        query: str | None = None,
    ) -> dict[str, Any]:
        if operation == "restart":
            with self._lock:
                clients = list(self.clients.values())
                self.clients.clear()
            for client in clients:
                client.close()
            return {
                "ok": True,
                "operation": operation,
                "servers": sorted(self.servers),
            }

        operations = {
            "goToDefinition": "textDocument/definition",
            "findReferences": "textDocument/references",
            "goToImplementation": "textDocument/implementation",
            "documentSymbol": "textDocument/documentSymbol",
            "workspaceSymbol": "workspace/symbol",
        }
        method = operations.get(operation)
        if method is None:
            return {
                "ok": False,
                "operation": operation,
                "error": f"Unknown LSP operation: {operation}",
            }

        if operation == "workspaceSymbol":
            if not isinstance(query, str) or not query.strip():
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "workspaceSymbol requires query",
                }
            if not self.servers:
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "No LSP servers configured",
                }
            raw_results: list[Any] = []
            errors: list[str] = []
            succeeded = False
            for name, config in self.servers.items():
                try:
                    with self._lock:
                        client = self.clients.get(name)
                        if client is None:
                            client = LspClient(name, config, self.workspace)
                            client.initialize()
                            self.clients[name] = client
                    result = client.request(method, {"query": query})
                    succeeded = True
                    if isinstance(result, list):
                        raw_results.extend(result)
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
                    if client._reader_error is not None or (
                        client.process is not None and client.process.poll() is not None
                    ):
                        with self._lock:
                            if self.clients.get(name) is client:
                                self.clients.pop(name)
                        client.close()
            if not succeeded:
                return {
                    "ok": False,
                    "operation": operation,
                    "error": "; ".join(errors),
                }
            uri = None
        else:
            if not isinstance(file_path, str) or not file_path:
                return {
                    "ok": False,
                    "operation": operation,
                    "error": f"{operation} requires file_path",
                }
            path = Path(file_path)
            if not path.is_absolute():
                path = self.workspace / path
            path = path.resolve()
            if operation in {
                "goToDefinition",
                "findReferences",
                "goToImplementation",
            } and (
                not isinstance(line, int)
                or isinstance(line, bool)
                or line < 0
                or not isinstance(character, int)
                or isinstance(character, bool)
                or character < 0
            ):
                return {
                    "ok": False,
                    "operation": operation,
                    "error": f"{operation} requires 0-based line and character",
                }
            route = self._extensions.get(path.suffix.lower())
            if route is None:
                return {
                    "ok": False,
                    "operation": operation,
                    "error": f"No LSP server configured for {path.suffix or path.name}",
                }
            name, language_id = route
            try:
                with self._lock:
                    client = self.clients.get(name)
                    if client is None:
                        client = LspClient(name, self.servers[name], self.workspace)
                        client.initialize()
                        self.clients[name] = client
                uri = client.sync_document(path, language_id)
                if operation in {
                    "goToDefinition",
                    "findReferences",
                    "goToImplementation",
                }:
                    params: dict[str, Any] = {
                        "textDocument": {"uri": uri},
                        "position": {"line": line, "character": character},
                    }
                    if operation == "findReferences":
                        params["context"] = {"includeDeclaration": True}
                else:
                    params = {"textDocument": {"uri": uri}}
                result = client.request(method, params)
                if isinstance(result, list):
                    raw_results = result
                elif isinstance(result, dict):
                    raw_results = [result]
                else:
                    raw_results = []
            except Exception as exc:
                if client._reader_error is not None or (
                    client.process is not None and client.process.poll() is not None
                ):
                    with self._lock:
                        if self.clients.get(name) is client:
                            self.clients.pop(name)
                    client.close()
                return {
                    "ok": False,
                    "operation": operation,
                    "error": f"{name}: {exc}",
                }

        formatted: list[dict[str, Any]] = []
        if operation in {
            "goToDefinition",
            "findReferences",
            "goToImplementation",
        }:
            for location in raw_results[:201]:
                if not isinstance(location, dict):
                    continue
                location_uri = location.get("targetUri", location.get("uri"))
                location_range = location.get(
                    "targetSelectionRange",
                    location.get("range"),
                )
                if not isinstance(location_uri, str) or not isinstance(location_range, dict):
                    continue
                start = location_range.get("start")
                if not isinstance(start, dict):
                    continue
                parsed = urlparse(location_uri)
                result_path = location_uri
                if parsed.scheme == "file":
                    result_path = unquote(parsed.path)
                    if parsed.netloc:
                        result_path = f"//{parsed.netloc}{result_path}"
                    if (
                        os.name == "nt"
                        and len(result_path) > 2
                        and result_path[0] == "/"
                        and result_path[2] == ":"
                    ):
                        result_path = result_path[1:]
                    try:
                        result_path = Path(result_path).resolve().relative_to(
                            self.workspace
                        ).as_posix()
                    except ValueError:
                        pass
                formatted.append(
                    {
                        "path": result_path,
                        "line": int(start.get("line", 0)) + 1,
                        "character": int(start.get("character", 0)) + 1,
                    }
                )
        else:
            stack: list[tuple[Any, str | None]] = [
                (symbol, None) for symbol in reversed(raw_results)
            ]
            while stack and len(formatted) < 201:
                symbol, parent = stack.pop()
                if not isinstance(symbol, dict):
                    continue
                location = symbol.get("location")
                location_uri = uri
                location_range = symbol.get("selectionRange", symbol.get("range"))
                if isinstance(location, dict):
                    location_uri = location.get("uri", location_uri)
                    location_range = location.get("range", location_range)
                if not isinstance(location_uri, str):
                    continue
                parsed = urlparse(location_uri)
                result_path = location_uri
                if parsed.scheme == "file":
                    result_path = unquote(parsed.path)
                    if parsed.netloc:
                        result_path = f"//{parsed.netloc}{result_path}"
                    if (
                        os.name == "nt"
                        and len(result_path) > 2
                        and result_path[0] == "/"
                        and result_path[2] == ":"
                    ):
                        result_path = result_path[1:]
                    try:
                        result_path = Path(result_path).resolve().relative_to(
                            self.workspace
                        ).as_posix()
                    except ValueError:
                        pass
                item: dict[str, Any] = {
                    "name": str(symbol.get("name", "")),
                    "kind": symbol.get("kind"),
                    "path": result_path,
                }
                if isinstance(location_range, dict) and isinstance(
                    location_range.get("start"), dict
                ):
                    start = location_range["start"]
                    item["line"] = int(start.get("line", 0)) + 1
                    item["character"] = int(start.get("character", 0)) + 1
                container = symbol.get("containerName", parent)
                if container:
                    item["container"] = str(container)
                formatted.append(item)
                children = symbol.get("children")
                if isinstance(children, list):
                    stack.extend(
                        (child, str(symbol.get("name", "")))
                        for child in reversed(children)
                    )

        result = {
            "ok": True,
            "operation": operation,
            "count": min(len(formatted), 200),
            "truncated": len(formatted) > 200 or len(raw_results) > 200,
        }
        result[
            "locations"
            if operation
            in {"goToDefinition", "findReferences", "goToImplementation"}
            else "symbols"
        ] = formatted[:200]
        return result

    def close(self) -> None:
        with self._lock:
            clients = list(self.clients.values())
            self.clients.clear()
        for client in clients:
            client.close()
