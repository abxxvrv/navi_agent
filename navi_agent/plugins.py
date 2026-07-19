from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


def discover_plugins(
    workspace: Path,
    navi_home: Path,
    config: dict[str, Any],
    plugin_dirs: list[Path] | None = None,
) -> list[dict[str, Any]]:
    """Discover plugins in precedence order and load their component metadata."""
    workspace = workspace.resolve()
    navi_home = navi_home.resolve()
    plugin_config = config.get("plugins", {})
    if not isinstance(plugin_config, dict):
        plugin_config = {}
    enabled = set(plugin_config.get("enabled", []))
    disabled = set(plugin_config.get("disabled", []))

    trusted_paths: set[Path] = set()
    trust_file = navi_home / "trusted-plugins"
    if trust_file.is_file():
        for line in trust_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                trusted_paths.add(Path(line).expanduser().resolve())

    candidates: list[tuple[Path, str, bool, bool]] = []
    for path in plugin_dirs or []:
        candidates.append((Path(path).expanduser(), "cli", True, True))

    chain = [workspace]
    current = workspace
    while not (current / ".git").exists() and current.parent != current:
        current = current.parent
        chain.append(current)
    if not (current / ".git").exists():
        chain = [workspace]
    for directory in chain:
        for parent in (
            directory / ".navi" / "plugins",
            directory / ".grok" / "plugins",
            directory / ".claude" / "plugins",
        ):
            if parent.is_dir():
                for path in sorted(parent.iterdir(), key=lambda item: item.name):
                    if path.is_dir():
                        candidates.append((path, "project", False, False))

    for parent in (navi_home / "plugins", Path.home() / ".claude" / "plugins"):
        if parent.is_dir():
            for path in sorted(parent.iterdir(), key=lambda item: item.name):
                if path.is_dir():
                    candidates.append((path, "user", False, True))

    config_paths = plugin_config.get("paths", [])
    if isinstance(config_paths, str):
        config_paths = [config_paths]
    if isinstance(config_paths, list):
        for value in config_paths:
            if isinstance(value, str):
                path = Path(value).expanduser()
                if not path.is_absolute():
                    path = workspace / path
                candidates.append((path, "config", True, False))

    plugins: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()
    seen_names: set[str] = set()
    for candidate, scope, default_enabled, default_trusted in candidates:
        if not candidate.is_dir():
            logger.warning("Plugin path is not a directory: %s", candidate)
            continue
        root = candidate.resolve()
        if root in seen_paths:
            continue
        seen_paths.add(root)

        manifest: dict[str, Any] = {}
        manifest_path = next(
            (
                path
                for path in (
                    root / "plugin.json",
                    root / ".grok-plugin" / "plugin.json",
                    root / ".claude-plugin" / "plugin.json",
                )
                if path.is_file()
            ),
            None,
        )
        if manifest_path is not None:
            try:
                loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Invalid plugin manifest %s: %s", manifest_path, exc)
                continue
            if not isinstance(loaded, dict):
                logger.warning("Plugin manifest must be an object: %s", manifest_path)
                continue
            manifest = loaded

        if manifest_path is not None:
            name = manifest.get("name")
        else:
            name = "".join(
                char if char.isascii() and (char.isalnum() or char == "-") else "-"
                for char in root.name.lower()
            ).strip("-")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,62}[a-z0-9])?", name):
            logger.warning("Invalid plugin name %r in %s", name, root)
            continue
        if name in seen_names:
            logger.warning("Skipping lower-priority duplicate plugin %s at %s", name, root)
            continue

        plugin_id = f"{scope}/{hashlib.sha256(str(root).encode()).hexdigest()[:8]}/{name}"
        is_enabled = default_enabled or name in enabled or plugin_id in enabled
        if name in disabled or plugin_id in disabled:
            is_enabled = False
        is_trusted = default_trusted or root in trusted_paths
        if scope == "config" and root.is_relative_to(navi_home):
            is_trusted = True
        data_dir = navi_home / "plugin-data" / plugin_id

        skill_files: list[Path] = []
        command_files: list[Path] = []
        agent_files: list[Path] = []
        for field, default, target, pattern in (
            ("skills", "skills", skill_files, "SKILL.md"),
            ("commands", "commands", command_files, "*.md"),
            ("agents", "agents", agent_files, "*.md"),
        ):
            values = manifest.get(field, default)
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                logger.warning("Plugin %s has invalid %s component", name, field)
                continue
            for value in values:
                if not isinstance(value, str):
                    logger.warning("Plugin %s has a non-path %s component", name, field)
                    continue
                path = (root / value).resolve()
                if not path.is_relative_to(root):
                    logger.warning("Plugin %s %s path escapes its root: %s", name, field, value)
                    continue
                if field == "skills":
                    if path.is_file() and path.name == "SKILL.md":
                        target.append(path)
                    elif path.is_dir() and (path / "SKILL.md").is_file():
                        skill_file = (path / "SKILL.md").resolve()
                        if skill_file.is_relative_to(root):
                            target.append(skill_file)
                    elif path.is_dir():
                        for skill_file in sorted(path.rglob(pattern)):
                            skill_file = skill_file.resolve()
                            if skill_file.is_relative_to(root) and skill_file.is_file():
                                target.append(skill_file)
                elif path.is_file() and path.suffix == ".md":
                    target.append(path)
                elif path.is_dir():
                    for markdown_file in sorted(path.glob(pattern)):
                        markdown_file = markdown_file.resolve()
                        if markdown_file.is_relative_to(root) and markdown_file.is_file():
                            target.append(markdown_file)

        tokens = {
            "${GROK_PLUGIN_ROOT}": str(root),
            "${CLAUDE_PLUGIN_ROOT}": str(root),
            "${GROK_PLUGIN_DATA}": str(data_dir),
            "${CLAUDE_PLUGIN_DATA}": str(data_dir),
        }
        components: dict[str, Any] = {}
        for field, default_file in (
            ("hooks", "hooks/hooks.json"),
            ("mcpServers", ".mcp.json"),
            ("lspServers", ".lsp.json"),
        ):
            value = manifest.get(field)
            base = root
            if isinstance(value, str):
                path = (root / value).resolve()
                if not path.is_relative_to(root):
                    logger.warning("Plugin %s %s path escapes its root: %s", name, field, value)
                    continue
                if not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    base = path.parent
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid plugin component %s: %s", path, exc)
                    continue
            elif value is None:
                path = (root / default_file).resolve()
                if not path.is_relative_to(root) or not path.is_file():
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                    base = path.parent
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid plugin component %s: %s", path, exc)
                    continue
            if not isinstance(value, dict):
                logger.warning("Plugin %s has invalid %s component", name, field)
                continue
            encoded = json.dumps(value)
            for token, replacement in tokens.items():
                encoded = encoded.replace(token, json.dumps(replacement)[1:-1])
            value = json.loads(encoded)
            if field == "hooks":
                components["hooks"] = value
                components["hooks_base"] = base
            else:
                key = "mcp_servers" if field == "mcpServers" else "lsp_servers"
                wrapped = value.get(field)
                components[key] = wrapped if isinstance(wrapped, dict) else value

        default_mcp_path = (root / ".mcp.json").resolve()
        if (
            isinstance(manifest.get("mcpServers"), dict)
            and default_mcp_path.is_relative_to(root)
            and default_mcp_path.is_file()
        ):
            try:
                file_value = json.loads(default_mcp_path.read_text(encoding="utf-8"))
                encoded = json.dumps(file_value)
                for token, replacement in tokens.items():
                    encoded = encoded.replace(token, json.dumps(replacement)[1:-1])
                file_value = json.loads(encoded)
                file_servers = file_value.get("mcpServers", file_value) if isinstance(file_value, dict) else {}
                if isinstance(file_servers, dict):
                    inline_servers = components.get("mcp_servers", {})
                    components["mcp_servers"] = dict(file_servers)
                    for server_name, server in inline_servers.items():
                        components["mcp_servers"].setdefault(server_name, server)
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Invalid plugin component %s: %s", default_mcp_path, exc)

        for key in ("mcp_servers", "lsp_servers"):
            servers = components.get(key)
            if isinstance(servers, dict):
                components[key] = {
                    server_name: server
                    for server_name, server in servers.items()
                    if isinstance(server_name, str) and isinstance(server, dict)
                }

        skills: dict[str, dict[str, Any]] = {}
        commands: dict[str, str] = {}
        agents: dict[str, dict[str, Any]] = {}
        for kind, paths in (
            ("skill", skill_files),
            ("command", command_files),
            ("agent", agent_files),
        ):
            if kind == "agent" and not is_trusted:
                continue
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                    metadata: dict[str, Any] = {}
                    body = text.strip()
                    lines = text.lstrip().splitlines()
                    if lines and lines[0].strip() == "---":
                        closing = next(
                            (i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"),
                            None,
                        )
                        if closing is not None:
                            loaded = yaml.safe_load("\n".join(lines[1:closing])) or {}
                            if isinstance(loaded, dict):
                                metadata = loaded
                            body = "\n".join(lines[closing + 1 :]).strip()
                except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
                    logger.warning("Invalid plugin %s %s: %s", kind, path, exc)
                    continue
                for token, replacement in tokens.items():
                    body = body.replace(token, replacement)
                if kind in {"skill", "command"}:
                    body = body.replace("${SKILL_DIR}", str(path.parent)).replace(
                        "${CLAUDE_SKILL_DIR}",
                        str(path.parent),
                    )
                    basename = path.parent.name if kind == "skill" else path.stem
                    component_name = re.sub(r"[^a-z0-9]+", "-", basename.lower()).strip("-")
                    if not component_name:
                        continue
                    disable_model_invocation = metadata.get("disable-model-invocation")
                    if not (
                        disable_model_invocation is True
                        or disable_model_invocation == "true"
                    ):
                        skills.setdefault(
                            component_name,
                            {"path": path, "root": root, "data_dir": data_dir},
                        )
                    user_invocable = metadata.get(
                        "user-invocable",
                        metadata.get("userInvocable"),
                    )
                    if (
                        user_invocable is None
                        or user_invocable is True
                        or user_invocable == "true"
                    ):
                        commands.setdefault(component_name, body)
                elif body:
                    component_name = metadata.get("name") or path.stem
                    if not isinstance(component_name, str) or not component_name:
                        continue
                    tools = metadata.get("tools", [])
                    denied_tools = metadata.get("disallowedTools", [])
                    if isinstance(tools, str):
                        tools = [name.strip() for name in tools.split(",") if name.strip()]
                    if isinstance(denied_tools, str):
                        denied_tools = [
                            name.strip() for name in denied_tools.split(",") if name.strip()
                        ]
                    prompt_mode = metadata.get("promptMode", "extend")
                    if prompt_mode not in {"extend", "full"}:
                        logger.warning("Plugin %s agent %s has invalid promptMode", name, path)
                        continue
                    agents.setdefault(
                        component_name,
                        {
                            "description": str(metadata.get("description", "")),
                            "prompt": body,
                            "tools": tools if isinstance(tools, list) else [],
                            "disallowed_tools": (
                                denied_tools if isinstance(denied_tools, list) else []
                            ),
                            "prompt_mode": prompt_mode,
                        },
                    )

        if manifest_path is None and not any(
            (skill_files, command_files, agent_files, components)
        ):
            continue
        if is_enabled and is_trusted:
            data_dir.mkdir(parents=True, exist_ok=True)
        seen_names.add(name)
        plugins.append(
            {
                "name": name,
                "id": plugin_id,
                "root": root,
                "data_dir": data_dir,
                "scope": scope,
                "enabled": is_enabled,
                "trusted": is_trusted,
                "version": manifest.get("version"),
                "description": manifest.get("description", ""),
                "mcp_servers": components.get("mcp_servers", {}),
                "lsp_servers": components.get("lsp_servers", {}),
                "hooks": components.get("hooks"),
                "hooks_base": components.get("hooks_base"),
                "skills": skills,
                "commands": commands,
                "agents": agents,
            }
        )

    return plugins
