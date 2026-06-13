"""
MCP CLI Commands

Handles /mcp subcommands for managing MCP servers:
- /mcp list     - 列出所有 server 状态
- /mcp add      - 添加 server（交互式）
- /mcp remove   - 移除 server
- /mcp reload   - 重新连接
- /mcp status   - 显示详细状态
"""

import json
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def handle_mcp_command(args: str, registry, console=None) -> str:
    """分发 /mcp 子命令。

    Args:
        args: 命令参数字符串
        registry: ToolRegistry 实例
        console: Rich Console 实例（可选）

    Returns:
        命令执行结果的文本描述
    """
    from .mcp_client import reload_mcp_servers

    parts = args.strip().split(None, 1)
    subcommand = parts[0].lower() if parts else "status"
    subargs = parts[1] if len(parts) > 1 else ""

    if subcommand in ("list", "status"):
        return _mcp_status()

    elif subcommand == "add":
        return _mcp_add(subargs)

    elif subcommand in ("remove", "rm"):
        return _mcp_remove(subargs, registry)

    elif subcommand == "reload":
        return reload_mcp_servers(registry)

    elif subcommand == "help":
        return _mcp_help()

    else:
        return f"Unknown /mcp subcommand: {subcommand}\n" + _mcp_help()


def _mcp_help() -> str:
    """返回 /mcp 帮助信息。"""
    return """Usage: /mcp <subcommand> [args]

Subcommands:
  status          Show MCP server status (default)
  add <name>      Add a new MCP server (interactive)
  remove <name>   Remove an MCP server
  reload          Reload all MCP connections
  help            Show this help message

Config file: ~/.navi/config.json (mcp_servers section)
"""


def _mcp_status() -> str:
    """显示 MCP 服务器状态。"""
    from .mcp_client import get_mcp_status, _MCP_AVAILABLE

    if not _MCP_AVAILABLE:
        return "MCP SDK not installed. Install with: pip install mcp"

    status_list = get_mcp_status()
    if not status_list:
        return "No MCP servers configured. Use /mcp add to add one."

    lines = ["MCP Servers:"]
    lines.append("-" * 50)

    for s in status_list:
        status_icon = "✓" if s["connected"] else "✗"
        tools_info = f"{s['tools']} tool(s)" if s["connected"] else "disconnected"
        lines.append(f"  {status_icon} {s['name']:<20} [{s['transport']}] {tools_info}")

    lines.append("-" * 50)
    total_tools = sum(s["tools"] for s in status_list)
    connected = sum(1 for s in status_list if s["connected"])
    lines.append(f"  Total: {len(status_list)} server(s), {connected} connected, {total_tools} tool(s)")

    return "\n".join(lines)


def _mcp_add(args: str) -> str:
    """交互式添加 MCP 服务器。"""
    from .mcp_client import _sanitize_name, _MCP_AVAILABLE

    if not _MCP_AVAILABLE:
        return "MCP SDK not installed. Install with: pip install mcp"

    # Parse name from args
    name = args.strip()
    if not name:
        return "Usage: /mcp add <server_name>"

    # Sanitize name
    name = _sanitize_name(name)

    # Load existing config
    config_path = _get_config_path()
    config = _load_config_file(config_path)

    if "mcp_servers" not in config:
        config["mcp_servers"] = {}

    if name in config["mcp_servers"]:
        return f"MCP server '{name}' already exists. Use /mcp remove first."

    # Interactive setup
    print(f"\n  Adding MCP server: {name}")
    print("  " + "=" * 40)

    # Ask transport type
    transport = input("  Transport type (stdio/http) [stdio]: ").strip().lower()
    if not transport:
        transport = "stdio"

    server_config = {}

    if transport == "stdio":
        command = input("  Command (e.g., npx, uvx): ").strip()
        if not command:
            return "Command is required for stdio transport."

        args_str = input("  Args (space-separated, optional): ").strip()
        args_list = args_str.split() if args_str else []

        server_config["command"] = command
        if args_list:
            server_config["args"] = args_list

        # Optional env vars
        env_str = input("  Env vars (KEY=VALUE, comma-separated, optional): ").strip()
        if env_str:
            env = {}
            for pair in env_str.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    env[k.strip()] = v.strip()
            if env:
                server_config["env"] = env

    elif transport == "http":
        url = input("  URL (e.g., http://localhost:8000/sse): ").strip()
        if not url:
            return "URL is required for HTTP transport."

        server_config["url"] = url

        headers_str = input("  Headers (KEY=VALUE, comma-separated, optional): ").strip()
        if headers_str:
            headers = {}
            for pair in headers_str.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    headers[k.strip()] = v.strip()
            if headers:
                server_config["headers"] = headers

        # Ask if SSE
        use_sse = input("  Use SSE transport? (y/N): ").strip().lower()
        if use_sse in ("y", "yes"):
            server_config["transport"] = "sse"

    else:
        return f"Unknown transport type: {transport}"

    # Optional timeout
    timeout_str = input("  Timeout in seconds [120]: ").strip()
    if timeout_str:
        try:
            server_config["timeout"] = int(timeout_str)
        except ValueError:
            pass

    # Save to config
    config["mcp_servers"][name] = server_config
    _save_config_file(config_path, config)

    return f"MCP server '{name}' added. Use /mcp reload to connect."


def _mcp_remove(args: str, registry) -> str:
    """移除 MCP 服务器。"""
    name = args.strip()
    if not name:
        return "Usage: /mcp remove <server_name>"

    config_path = _get_config_path()
    config = _load_config_file(config_path)

    if "mcp_servers" not in config or name not in config["mcp_servers"]:
        return f"MCP server '{name}' not found in config."

    # Remove from config
    del config["mcp_servers"][name]
    if not config["mcp_servers"]:
        del config["mcp_servers"]

    _save_config_file(config_path, config)

    # Disconnect if connected and remove tools from registry
    from .mcp_client import _servers, _lock, _run_on_mcp_loop, _sanitize_name
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
        logger.info("MCP server '%s' disconnected, removed %d tool(s)", name, removed)

    return f"MCP server '{name}' removed."


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _get_config_path() -> Path:
    """获取 config.json 路径。"""
    from ..paths import get_config_path
    return get_config_path()


def _load_config_file(path: Path) -> dict:
    """加载 config.json。"""
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config_file(path: Path, config: dict):
    """保存 config.json。"""
    path.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")