import json
from pathlib import Path

import pytest

from navi_agent.context.context_manager import ContextManager
from navi_agent.integrations import mcp_client
from navi_agent.plugins import discover_plugins
from navi_agent.runtime.agent import AgentRuntime
from navi_agent.tools.builtin import SkillViewTool


def test_manifest_priority_and_convention_discovery(tmp_path):
    home = tmp_path / "home"
    explicit = tmp_path / "explicit"
    (explicit / ".grok-plugin").mkdir(parents=True)
    (explicit / "skills" / "one").mkdir(parents=True)
    (explicit / "plugin.json").write_text('{"name":"primary"}', encoding="utf-8")
    (explicit / ".grok-plugin" / "plugin.json").write_text(
        '{"name":"secondary"}', encoding="utf-8"
    )
    (explicit / "skills" / "one" / "SKILL.md").write_text("body", encoding="utf-8")

    conventional = tmp_path / "Conventional_Plugin"
    (conventional / "commands").mkdir(parents=True)
    (conventional / "commands" / "check.md").write_text("check", encoding="utf-8")
    (conventional / "commands" / "manual.md").write_text(
        "---\ndisable-model-invocation: true\n---\nmanual",
        encoding="utf-8",
    )

    plugins = discover_plugins(tmp_path, home, {}, [explicit, conventional])

    assert [plugin["name"] for plugin in plugins] == ["primary", "conventional-plugin"]
    assert plugins[0]["skills"]["one"]["path"] == explicit / "skills" / "one" / "SKILL.md"
    assert plugins[0]["commands"] == {"one": "body"}
    assert plugins[1]["skills"]["check"]["path"] == conventional / "commands" / "check.md"
    assert plugins[1]["commands"] == {"check": "check", "manual": "manual"}
    assert "manual" not in plugins[1]["skills"]


def test_discovery_precedence_enablement_and_trust(tmp_path):
    workspace = tmp_path / "repo"
    home = tmp_path / "home"
    (workspace / ".git").mkdir(parents=True)

    cli = tmp_path / "cli"
    project_duplicate = workspace / ".navi" / "plugins" / "duplicate"
    project = workspace / ".navi" / "plugins" / "project"
    project_off = workspace / ".navi" / "plugins" / "project-off"
    user = home / "plugins" / "user"
    config = tmp_path / "configured"
    for root, name in (
        (cli, "duplicate"),
        (project_duplicate, "duplicate"),
        (project, "project"),
        (project_off, "project-off"),
        (user, "user"),
        (config, "configured"),
    ):
        root.mkdir(parents=True)
        (root / "plugin.json").write_text(json.dumps({"name": name}), encoding="utf-8")
    (project / "agents").mkdir()
    (project / "agents" / "unsafe.md").write_text(
        "---\nname: unsafe\ndescription: unsafe\n---\nrun commands",
        encoding="utf-8",
    )

    plugins = discover_plugins(
        workspace,
        home,
        {
            "plugins": {
                "enabled": ["project", "user"],
                "paths": [str(config)],
            }
        },
        [cli],
    )
    by_name = {plugin["name"]: plugin for plugin in plugins}

    assert by_name["duplicate"]["root"] == cli.resolve()
    assert (by_name["duplicate"]["scope"], by_name["duplicate"]["enabled"], by_name["duplicate"]["trusted"]) == (
        "cli",
        True,
        True,
    )
    assert (by_name["project"]["scope"], by_name["project"]["enabled"], by_name["project"]["trusted"]) == (
        "project",
        True,
        False,
    )
    assert by_name["project-off"]["enabled"] is False
    assert by_name["project"]["agents"] == {}
    assert (by_name["user"]["scope"], by_name["user"]["enabled"], by_name["user"]["trusted"]) == (
        "user",
        True,
        True,
    )
    assert (by_name["configured"]["scope"], by_name["configured"]["enabled"], by_name["configured"]["trusted"]) == (
        "config",
        True,
        False,
    )


def test_component_paths_cannot_escape_plugin_root(tmp_path):
    root = tmp_path / "plugin"
    root.mkdir()
    outside_skill = tmp_path / "outside-skill"
    outside_skill.mkdir()
    (outside_skill / "SKILL.md").write_text("outside", encoding="utf-8")
    outside_json = tmp_path / "outside.json"
    outside_json.write_text('{"outside": {}}', encoding="utf-8")
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "secure",
                "skills": "../outside-skill",
                "commands": "../outside-skill",
                "agents": "../outside-skill",
                "hooks": "../outside.json",
                "mcpServers": "../outside.json",
                "lspServers": "../outside.json",
            }
        ),
        encoding="utf-8",
    )

    plugin = discover_plugins(tmp_path, tmp_path / "home", {}, [root])[0]

    assert plugin["skills"] == {}
    assert plugin["commands"] == {}
    assert plugin["agents"] == {}
    assert plugin["hooks"] is None
    assert plugin["mcp_servers"] == {}
    assert plugin["lsp_servers"] == {}


def test_component_symlinks_cannot_escape_plugin_root(tmp_path):
    root = tmp_path / "plugin"
    (root / "skills" / "linked").mkdir(parents=True)
    (root / "commands").mkdir()
    (root / "hooks").mkdir()
    outside_markdown = tmp_path / "outside.md"
    outside_json = tmp_path / "outside.json"
    outside_markdown.write_text("secret", encoding="utf-8")
    outside_json.write_text('{"mcpServers":{"bad":{"command":"bad"}}}', encoding="utf-8")
    try:
        (root / "skills" / "linked" / "SKILL.md").symlink_to(outside_markdown)
        (root / "commands" / "bad.md").symlink_to(outside_markdown)
        (root / ".mcp.json").symlink_to(outside_json)
        (root / ".lsp.json").symlink_to(outside_json)
        (root / "hooks" / "hooks.json").symlink_to(outside_json)
    except OSError:
        pytest.skip("symlinks are unavailable")
    (root / "plugin.json").write_text('{"name":"linked"}', encoding="utf-8")

    plugin = discover_plugins(tmp_path, tmp_path / "home", {}, [root])[0]

    assert plugin["skills"] == {}
    assert plugin["commands"] == {}
    assert plugin["mcp_servers"] == {}
    assert plugin["lsp_servers"] == {}
    assert plugin["hooks"] is None
    result = SkillViewTool(
        tmp_path,
        plugin_skills={
            "linked:bad": {
                "path": outside_markdown,
                "root": root,
                "data_dir": tmp_path / "data",
            }
        },
    )("linked:bad")
    assert result["ok"] is False


def test_plugin_skill_index_and_token_expansion(tmp_path):
    root = tmp_path / "plugin"
    skill_file = root / "skills" / "inspect" / "SKILL.md"
    skill_file.parent.mkdir(parents=True)
    skill_file.write_text(
        "---\nname: ignored\ndescription: Inspect a project\n---\n"
        "root=${GROK_PLUGIN_ROOT}\ndata=${CLAUDE_PLUGIN_DATA}\n"
        "skill=${SKILL_DIR}\nsession=${CLAUDE_SESSION_ID}\n",
        encoding="utf-8",
    )
    plugin = discover_plugins(tmp_path, tmp_path / "home", {}, [root])[0]
    plugin_skills = {
        "plugin:inspect": {
            "path": skill_file,
            "root": plugin["root"],
            "data_dir": plugin["data_dir"],
        }
    }

    index = ContextManager(
        workspace=tmp_path,
        skills_path=str(tmp_path / "missing-native-skills"),
        plugin_skills=plugin_skills,
    ).scan_skill_index()
    result = SkillViewTool(
        tmp_path,
        plugin_skills=plugin_skills,
        session_id="session-1",
    )("plugin:inspect")

    assert index == [
        {
            "name": "plugin:inspect",
            "description": "Inspect a project",
            "path": str(skill_file),
        }
    ]
    assert result["ok"] is True
    assert f"root={root.resolve()}" in result["content"]
    assert f"data={plugin['data_dir']}" in result["content"]
    assert f"skill={skill_file.parent}" in result["content"]
    assert "session=session-1" in result["content"]
    assert "${" not in result["content"]


def test_components_are_exposed_and_global_mcp_wins(monkeypatch, tmp_path):
    home = tmp_path / "home"
    root = tmp_path / "plugin"
    (root / "commands").mkdir(parents=True)
    (root / "agents").mkdir()
    (root / "hooks").mkdir()
    (root / "commands" / "deploy.md").write_text(
        "---\nname: ship\n---\nDeploy from ${GROK_PLUGIN_ROOT}", encoding="utf-8"
    )
    (root / "agents" / "review.md").write_text(
        "---\nname: reviewer\ndescription: Review code\ntools: Read, Grep\n---\n"
        "Review using ${CLAUDE_PLUGIN_DATA}",
        encoding="utf-8",
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "shared": {"command": "plugin"},
                    "plugin-only": {"command": "${GROK_PLUGIN_ROOT}/server"},
                    "bad": "not-an-object",
                }
            }
        ),
        encoding="utf-8",
    )
    (root / ".lsp.json").write_text(
        '{"lspServers":{"python":{"command":"pyright-langserver"}}}', encoding="utf-8"
    )
    hook_config = {"hooks": {"PreToolUse": [{"command": "check"}]}}
    (root / "hooks" / "hooks.json").write_text(json.dumps(hook_config), encoding="utf-8")
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "name": "bundle",
                "description": "All components",
                "mcpServers": {
                    "shared": {"command": "inline"},
                    "inline-only": {"command": "inline-server"},
                },
            }
        ),
        encoding="utf-8",
    )

    discovered = discover_plugins(tmp_path, home, {}, [root])[0]
    assert discovered["skills"]["deploy"]["path"] == root / "commands" / "deploy.md"
    assert discovered["commands"]["deploy"] == f"Deploy from {root.resolve()}"
    assert discovered["agents"]["reviewer"]["prompt"].startswith("Review using ")
    assert discovered["agents"]["reviewer"]["tools"] == ["Read", "Grep"]
    assert discovered["agents"]["reviewer"]["prompt_mode"] == "extend"
    assert discovered["mcp_servers"]["plugin-only"]["command"] == f"{root.resolve()}/server"
    assert discovered["mcp_servers"]["shared"] == {"command": "plugin"}
    assert discovered["mcp_servers"]["inline-only"] == {"command": "inline-server"}
    assert "bad" not in discovered["mcp_servers"]
    assert discovered["lsp_servers"] == {"python": {"command": "pyright-langserver"}}
    assert discovered["hooks"] == hook_config

    home.mkdir(exist_ok=True)
    (home / "config.json").write_text(
        json.dumps(
            {
                "default_provider": "lmstudio",
                "default_model": "dummy",
                "providers": {
                    "lmstudio": {
                        "api_key": "lm-studio",
                        "base_url": "http://localhost:1234/v1",
                        "models": {"dummy": {"context_window": 32768}},
                    }
                },
                "compression": {"provider": "lmstudio", "model": "dummy"},
                "mcp_servers": {"shared": {"command": "global"}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("NAVI_HOME", str(home))
    monkeypatch.setattr(AgentRuntime, "_init_mcp_tools", lambda self: None)

    runtime = AgentRuntime(workspace=tmp_path, plugin_dirs=[root])

    assert runtime.plugin_commands["bundle:deploy"] == f"Deploy from {root.resolve()}"
    assert runtime.plugin_agents["bundle:reviewer"]["prompt"].startswith("Review using ")
    assert "bundle:deploy" in {
        skill["name"] for skill in runtime.context_manager.scan_skill_index()
    }
    assert runtime.plugin_lsp_servers == discovered["lsp_servers"]
    assert runtime.plugin_hooks[0]["config"] == hook_config
    assert runtime.mcp_servers["shared"] == {"command": "global"}
    assert runtime.mcp_servers["plugin-only"] == discovered["mcp_servers"]["plugin-only"]


def test_malformed_mcp_server_does_not_hide_later_servers(monkeypatch):
    server = type("Server", (), {"_tools": [], "tool_timeout": 1})()
    mcp_client._servers.clear()
    monkeypatch.setattr(mcp_client, "_MCP_AVAILABLE", True)
    monkeypatch.setattr(mcp_client, "_ensure_mcp_loop", lambda: None)
    monkeypatch.setattr(mcp_client, "_connect_server", lambda _name, _config: server)
    monkeypatch.setattr(mcp_client, "_run_on_mcp_loop", lambda value, timeout: value)

    try:
        assert mcp_client.discover_mcp_tools(
            object(),
            {"bad": "invalid", "good": {"command": "server"}},
        ) == []
        assert "good" in mcp_client._servers
    finally:
        mcp_client._servers.clear()
