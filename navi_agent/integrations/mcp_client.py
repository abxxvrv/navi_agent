"""
MCP (Model Context Protocol) Client Support

Connects to external MCP servers via stdio or HTTP/SSE transport,
discovers their tools, and registers them into the Navi tool registry
so the agent can call them like any built-in tool.

Configuration is read from ~/.navi/config.json under the ``mcp_servers`` key.
The ``mcp`` Python package is optional -- if not installed, this module is a
no-op and logs a debug message.

Example config::

    {
      "mcp_servers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
          "timeout": 120
        },
        "github": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-github"],
          "env": {
            "GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_..."
          }
        },
        "remote_api": {
          "url": "https://my-mcp-server.example.com/mcp",
          "headers": {
            "Authorization": "Bearer sk-..."
          }
        }
      }
    }

Architecture:
    A dedicated background event loop (_mcp_loop) runs in a daemon thread.
    Each MCP server runs as a long-lived asyncio Task on this loop, keeping
    its transport context alive. Tool call coroutines are scheduled onto the
    loop via ``run_coroutine_threadsafe()``.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MCP SDK availability check
# ---------------------------------------------------------------------------

_MCP_AVAILABLE = False
_MCP_HTTP_AVAILABLE = False

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    _MCP_AVAILABLE = True
except ImportError:
    pass

try:
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_HTTP_AVAILABLE = True
except ImportError:
    pass

try:
    from mcp.client.sse import sse_client
    _MCP_SSE_AVAILABLE = True
except ImportError:
    _MCP_SSE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TOOL_TIMEOUT = 120.0
_DEFAULT_CONNECT_TIMEOUT = 60.0
_MAX_RECONNECT_ATTEMPTS = 3
_CIRCUIT_BREAKER_THRESHOLD = 5
_CIRCUIT_BREAKER_COOLDOWN_SEC = 60

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_servers: Dict[str, "MCPServerTask"] = {}
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None
_server_error_counts: Dict[str, int] = {}
_server_breaker_opened_at: Dict[str, float] = {}


# ---------------------------------------------------------------------------
# Env var interpolation
# ---------------------------------------------------------------------------

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


def _interpolate_env_vars(value: Any) -> Any:
    """Replace ``${ENV_VAR}`` patterns in string values with os.environ."""
    if isinstance(value, str) and "${" in value:
        def _replace(m):
            return os.environ.get(m.group(1), m.group(0))
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _interpolate_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate_env_vars(v) for v in value]
    return value


def _build_safe_env(user_env: Optional[dict]) -> dict:
    """Build a safe environment for stdio subprocess.

    Starts with a minimal base env and merges user-provided env vars.
    Prevents leaking the parent process's full environment.
    """
    safe = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", os.environ.get("USERPROFILE", "")),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "SystemRoot": os.environ.get("SystemRoot", ""),
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
    }
    if user_env:
        safe.update(user_env)
    return safe


# ---------------------------------------------------------------------------
# Name sanitization
# ---------------------------------------------------------------------------

def _sanitize_name(value: str) -> str:
    """Sanitize a name for use in tool names (alphanumeric + underscore)."""
    return re.sub(r"[^A-Za-z0-9_]", "_", str(value or ""))


# ---------------------------------------------------------------------------
# Schema conversion
# ---------------------------------------------------------------------------

def _normalize_mcp_input_schema(input_schema: Optional[dict]) -> dict:
    """Normalize MCP inputSchema to OpenAI function calling format.

    MCP uses JSON Schema; OpenAI expects a similar but slightly different
    format. This ensures compatibility.
    """
    if not input_schema:
        return {"type": "object", "properties": {}}

    schema = dict(input_schema)

    # Ensure type is present
    if "type" not in schema:
        schema["type"] = "object"

    # Remove MCP-specific fields that OpenAI doesn't understand
    for field in ["$schema", "$id", "$defs", "definitions"]:
        schema.pop(field, None)

    return schema


def _convert_mcp_schema(server_name: str, mcp_tool) -> dict:
    """Convert an MCP tool listing to the Navi registry schema format.

    Args:
        server_name: The logical server name for prefixing.
        mcp_tool:    An MCP ``Tool`` object with ``.name``, ``.description``,
                     and ``.inputSchema``.

    Returns:
        A dict with keys: name, description, parameters.
    """
    safe_tool_name = _sanitize_name(mcp_tool.name)
    safe_server_name = _sanitize_name(server_name)
    prefixed_name = f"mcp_{safe_server_name}_{safe_tool_name}"

    return {
        "name": prefixed_name,
        "description": mcp_tool.description or f"MCP tool {mcp_tool.name} from {server_name}",
        "parameters": _normalize_mcp_input_schema(getattr(mcp_tool, "inputSchema", None)),
    }


# ---------------------------------------------------------------------------
# MCPServerTask -- each MCP server lives in one long-lived asyncio Task
# ---------------------------------------------------------------------------

class MCPServerTask:
    """Manages a single MCP server connection in a dedicated asyncio Task.

    The entire connection lifecycle (connect, discover, serve, disconnect)
    runs inside one asyncio Task so that anyio cancel-scopes created by
    the transport client are entered and exited in the same Task context.

    Supports both stdio and HTTP/SSE transports.
    """

    def __init__(self, name: str):
        self.name = name
        self.session: Optional[Any] = None
        self.tool_timeout: float = _DEFAULT_TOOL_TIMEOUT
        self._task: Optional[asyncio.Task] = None
        self._ready = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._tools: list = []
        self._error: Optional[Exception] = None
        self._config: dict = {}
        self._registered_tool_names: list[str] = []
        self._rpc_lock = asyncio.Lock()

    def _is_http(self) -> bool:
        """Check if this server uses HTTP transport."""
        return "url" in self._config

    async def _discover_tools(self):
        """Fetch the tool list from the server."""
        async with self._rpc_lock:
            tools_result = await self.session.list_tools()
        self._tools = tools_result.tools if hasattr(tools_result, "tools") else []
        logger.info("MCP server '%s': discovered %d tool(s)", self.name, len(self._tools))

    async def _run_stdio(self, config: dict):
        """Run the server using stdio transport."""
        if not _MCP_AVAILABLE:
            raise ImportError("MCP SDK not installed. Install with: pip install mcp")

        command = config.get("command")
        args = config.get("args", [])
        user_env = config.get("env")

        if not command:
            raise ValueError(f"MCP server '{self.name}' has no 'command' in config")

        safe_env = _build_safe_env(user_env)

        server_params = StdioServerParameters(
            command=command,
            args=args,
            env=safe_env if safe_env else None,
        )

        try:
            async with stdio_client(server_params) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    self.session = session
                    await self._discover_tools()
                    self._ready.set()
                    # Wait until shutdown
                    await self._shutdown_event.wait()
        finally:
            self.session = None

    async def _run_http(self, config: dict):
        """Run the server using HTTP/StreamableHTTP or SSE transport."""
        url = config["url"]
        headers = config.get("headers", {})
        transport = config.get("transport", "http")

        if config.get("proxy", True):
            client_factory = None
        else:
            client_factory = lambda headers, timeout, auth: httpx.AsyncClient(
                headers=headers, timeout=timeout, auth=auth,
                follow_redirects=True, trust_env=False,
            )

        try:
            if transport == "sse":
                if not _MCP_SSE_AVAILABLE:
                    raise ImportError("SSE transport not available. Upgrade mcp package.")
                async with sse_client(url, headers=headers, httpx_client_factory=client_factory) if client_factory else sse_client(url, headers=headers) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        await self._shutdown_event.wait()
            else:
                if not _MCP_HTTP_AVAILABLE:
                    raise ImportError("HTTP transport not available. Upgrade mcp package.")
                async with streamablehttp_client(url, headers=headers, httpx_client_factory=client_factory) if client_factory else streamablehttp_client(url, headers=headers) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        self.session = session
                        await self._discover_tools()
                        self._ready.set()
                        await self._shutdown_event.wait()
        finally:
            self.session = None

    async def run(self, config: dict):
        """Main run loop with reconnection support."""
        self._config = config
        self.tool_timeout = config.get("timeout", _DEFAULT_TOOL_TIMEOUT)

        for attempt in range(_MAX_RECONNECT_ATTEMPTS):
            try:
                if self._is_http():
                    await self._run_http(config)
                else:
                    await self._run_stdio(config)
                return  # Clean exit
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._error = e
                if attempt < _MAX_RECONNECT_ATTEMPTS - 1:
                    wait = min(2 ** attempt, 10)
                    logger.warning(
                        "MCP server '%s' connection failed (attempt %d/%d): %s. Retrying in %ds...",
                        self.name, attempt + 1, _MAX_RECONNECT_ATTEMPTS, e, wait,
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error("MCP server '%s' failed after %d attempts: %s",
                               self.name, _MAX_RECONNECT_ATTEMPTS, e)
                    self._ready.set()  # Unblock caller even on failure
                    raise

    async def start(self, config: dict):
        """Create the background Task and wait until ready (or failed)."""
        self._task = asyncio.ensure_future(self.run(config))
        await self._ready.wait()

        if self._error and not self.session:
            raise self._error

    async def shutdown(self):
        """Signal the server to shut down gracefully."""
        self._shutdown_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.session = None


# ---------------------------------------------------------------------------
# Background event loop management
# ---------------------------------------------------------------------------

def _ensure_mcp_loop():
    """Start the background asyncio event loop if not already running."""
    global _mcp_loop, _mcp_thread

    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return

        _mcp_loop = asyncio.new_event_loop()

        def _run_loop():
            asyncio.set_event_loop(_mcp_loop)
            _mcp_loop.run_forever()

        _mcp_thread = threading.Thread(target=_run_loop, daemon=True, name="mcp-loop")
        _mcp_thread.start()


def _stop_mcp_loop():
    """Stop the background event loop."""
    global _mcp_loop, _mcp_thread

    with _lock:
        if _mcp_loop is not None:
            _mcp_loop.call_soon_threadsafe(_mcp_loop.stop)
            if _mcp_thread:
                _mcp_thread.join(timeout=5)
            _mcp_loop = None
            _mcp_thread = None


def _run_on_mcp_loop(coro, timeout: float = 120) -> Any:
    """Run an async coroutine on the background event loop (blocking)."""
    if _mcp_loop is None:
        raise RuntimeError("MCP event loop not started")

    future = asyncio.run_coroutine_threadsafe(coro, _mcp_loop)
    return future.result(timeout=timeout)


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

def _load_mcp_config() -> Dict[str, dict]:
    """Read ``mcp_servers`` from the Navi config file.

    Returns a dict of ``{server_name: server_config}`` or empty dict.
    """
    try:
        from ..paths import get_config_path
        config_path = get_config_path()
        if not config_path.is_file():
            return {}
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        servers = config.get("mcp_servers")
        if not servers or not isinstance(servers, dict):
            return {}
        return {name: _interpolate_env_vars(cfg) for name, cfg in servers.items()}
    except Exception as exc:
        logger.debug("Failed to load MCP config: %s", exc)
        return {}


async def _connect_server(name: str, config: dict) -> MCPServerTask:
    """Create an MCPServerTask, start it, and return when ready."""
    server = MCPServerTask(name)
    await server.start(config)
    return server


def _make_tool_handler(server_name: str, tool_name: str, tool_timeout: float):
    """Return a sync handler that calls an MCP tool via the background loop.

    The handler conforms to the registry's dispatch interface:
    ``handler(**kwargs) -> str``
    """

    def _handler(**kwargs) -> str:
        # Circuit breaker check
        if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
            opened_at = _server_breaker_opened_at.get(server_name, 0.0)
            age = time.monotonic() - opened_at
            if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
                remaining = max(1, int(_CIRCUIT_BREAKER_COOLDOWN_SEC - age))
                return json.dumps({
                    "error": (
                        f"MCP server '{server_name}' is unreachable after "
                        f"{_server_error_counts[server_name]} consecutive "
                        f"failures. Auto-retry available in ~{remaining}s."
                    )
                }, ensure_ascii=False)

        with _lock:
            server = _servers.get(server_name)
        if not server or not server.session:
            _bump_server_error(server_name)
            return json.dumps({
                "error": f"MCP server '{server_name}' is not connected"
            }, ensure_ascii=False)

        async def _call():
            async with server._rpc_lock:
                result = await server.session.call_tool(tool_name, arguments=kwargs)

            if result.isError:
                error_text = ""
                for block in (result.content or []):
                    if hasattr(block, "text"):
                        error_text += block.text
                return json.dumps({
                    "error": error_text or "MCP tool returned an error"
                }, ensure_ascii=False)

            parts: List[str] = []
            for block in (result.content or []):
                if hasattr(block, "text") and block.text:
                    parts.append(block.text)
            text_result = "\n".join(parts) if parts else ""

            return json.dumps({"result": text_result}, ensure_ascii=False)

        try:
            result = _run_on_mcp_loop(_call(), timeout=tool_timeout)
            # Check if the MCP tool itself returned an error
            try:
                parsed = json.loads(result)
                if "error" in parsed:
                    _bump_server_error(server_name)
                else:
                    _reset_server_error(server_name)
            except (json.JSONDecodeError, TypeError):
                _reset_server_error(server_name)
            return result
        except Exception as e:
            _bump_server_error(server_name)
            return json.dumps({
                "error": f"MCP tool call failed: {e}"
            }, ensure_ascii=False)

    return _handler


def _bump_server_error(server_name: str):
    """Increment error count and potentially open the circuit breaker."""
    with _lock:
        _server_error_counts[server_name] = _server_error_counts.get(server_name, 0) + 1
        if _server_error_counts[server_name] >= _CIRCUIT_BREAKER_THRESHOLD:
            _server_breaker_opened_at[server_name] = time.monotonic()
            logger.warning(
                "MCP server '%s': circuit breaker opened after %d failures",
                server_name, _server_error_counts[server_name],
            )


def _reset_server_error(server_name: str):
    """Reset error count on successful call."""
    with _lock:
        _server_error_counts.pop(server_name, None)
        _server_breaker_opened_at.pop(server_name, None)


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def _register_server_tools(
    name: str,
    server: MCPServerTask,
    registry,
) -> List[str]:
    """Register tools from an already-connected server into the registry.

    Returns:
        List of registered prefixed tool names.
    """
    registered_names: List[str] = []

    for mcp_tool in server._tools:
        schema = _convert_mcp_schema(name, mcp_tool)
        tool_name_prefixed = schema["name"]

        # Skip if already registered (e.g., from another server)
        if registry.has(tool_name_prefixed):
            logger.warning(
                "MCP server '%s': tool '%s' already registered — skipping",
                name, tool_name_prefixed,
            )
            continue

        handler = _make_tool_handler(name, mcp_tool.name, server.tool_timeout)

        registry.register(
            name=tool_name_prefixed,
            description=schema["description"],
            parameters=schema["parameters"],
            function=handler,
        )
        registered_names.append(tool_name_prefixed)

    return registered_names


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_mcp_tools(registry, servers: Dict[str, dict] | None = None) -> List[str]:
    """Entry point: load config, connect to MCP servers, register tools.

    Called from AgentRuntime.__init__. Safe to call even when the ``mcp``
    package is not installed (returns empty list).

    Args:
        registry: The ToolRegistry instance to register tools into.

    Returns:
        List of all registered MCP tool names.
    """
    if not _MCP_AVAILABLE:
        logger.debug("MCP SDK not available -- skipping MCP tool discovery")
        return []

    servers = (
        _load_mcp_config()
        if servers is None
        else {name: _interpolate_env_vars(cfg) for name, cfg in servers.items()}
    )
    if not servers:
        logger.debug("No MCP servers configured")
        return []

    _ensure_mcp_loop()

    all_tool_names: List[str] = []

    for server_name, server_config in servers.items():
        if not isinstance(server_config, dict):
            logger.warning("MCP server '%s' has invalid configuration, skipping", server_name)
            continue
        if server_config.get("enabled", True) is False:
            logger.debug("MCP server '%s' is disabled, skipping", server_name)
            continue

        if server_name in _servers:
            logger.debug("MCP server '%s' already connected", server_name)
            all_tool_names.extend(
                _register_server_tools(server_name, _servers[server_name], registry)
            )
            continue

        try:
            connect_timeout = server_config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            server = _run_on_mcp_loop(
                _connect_server(server_name, server_config),
                timeout=connect_timeout,
            )

            with _lock:
                _servers[server_name] = server

            tool_names = _register_server_tools(server_name, server, registry)
            server._registered_tool_names = tool_names
            all_tool_names.extend(tool_names)

            if tool_names:
                logger.info("MCP: %d tool(s) from server '%s'", len(tool_names), server_name)

        except Exception as e:
            logger.warning("Failed to connect to MCP server '%s': %s", server_name, e)

    return all_tool_names


def shutdown_mcp_servers():
    """Close all MCP server connections and stop the background loop."""
    with _lock:
        servers_snapshot = list(_servers.values())

    if not servers_snapshot:
        _stop_mcp_loop()
        return

    async def _shutdown():
        await asyncio.gather(
            *(server.shutdown() for server in servers_snapshot),
            return_exceptions=True,
        )

    try:
        _run_on_mcp_loop(_shutdown, timeout=10)
    except Exception:
        pass

    with _lock:
        _servers.clear()

    _stop_mcp_loop()


def reload_mcp_servers(registry, servers: Dict[str, dict] | None = None) -> str:
    """Reload MCP connections based on current config.

    Disconnects removed servers, connects new ones, and re-registers tools.

    Returns:
        A summary string of what changed.
    """
    if not _MCP_AVAILABLE:
        return "MCP SDK not installed"

    # Get current config
    servers_config = (
        _load_mcp_config()
        if servers is None
        else {name: _interpolate_env_vars(cfg) for name, cfg in servers.items()}
    )

    with _lock:
        current_names = set(_servers.keys())
        config_names = set(
            name for name, cfg in servers_config.items()
            if isinstance(cfg, dict) and cfg.get("enabled", True) is not False
        )

    # Disconnect servers no longer in config
    to_remove = current_names - config_names
    for name in to_remove:
        with _lock:
            server = _servers.pop(name, None)
        if server:
            try:
                _run_on_mcp_loop(server.shutdown(), timeout=5)
            except Exception:
                pass
            # Remove tools from registry
            prefix = f"mcp_{_sanitize_name(name)}_"
            removed = registry.remove_by_prefix(prefix)
            logger.info("MCP: removed server '%s' (%d tools)", name, removed)

    # Connect new servers
    to_add = config_names - current_names
    added_tools = 0
    for name in to_add:
        config = servers_config[name]
        try:
            connect_timeout = config.get("connect_timeout", _DEFAULT_CONNECT_TIMEOUT)
            server = _run_on_mcp_loop(
                _connect_server(name, config),
                timeout=connect_timeout,
            )

            with _lock:
                _servers[name] = server

            tool_names = _register_server_tools(name, server, registry)
            server._registered_tool_names = tool_names
            added_tools += len(tool_names)

        except Exception as e:
            logger.warning("Failed to connect to MCP server '%s': %s", name, e)

    # Summary
    parts = []
    if to_remove:
        parts.append(f"removed {len(to_remove)} server(s)")
    if to_add:
        parts.append(f"added {len(to_add)} server(s) with {added_tools} tool(s)")
    if not parts:
        parts.append("no changes")

    return "MCP reload: " + ", ".join(parts)


def get_mcp_status(configured: Dict[str, dict] | None = None) -> List[dict]:
    """Return status of all configured MCP servers.

    Returns a list of dicts with keys: name, transport, tools, connected.
    """
    result: List[dict] = []

    configured = _load_mcp_config() if configured is None else configured
    if not configured:
        return result

    with _lock:
        active_servers = dict(_servers)

    for name, cfg in configured.items():
        if not isinstance(cfg, dict):
            continue
        transport = "stdio" if "command" in cfg else cfg.get("transport", "http")
        server = active_servers.get(name)

        if server and server.session is not None:
            result.append({
                "name": name,
                "transport": transport,
                "tools": len(server._registered_tool_names),
                "connected": True,
            })
        else:
            result.append({
                "name": name,
                "transport": transport,
                "tools": 0,
                "connected": False,
            })

    return result
