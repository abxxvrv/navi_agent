from __future__ import annotations

import json
import os
import shlex
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from navi_agent.runtime.hooks import HookManager
from navi_agent.runtime.interrupt_scope import TurnScope


@pytest.mark.skipif(os.name == "nt", reason="test scripts use POSIX shebangs")
def test_loads_sources_in_order_and_resolves_each_relative_executable(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    hooks_dir = navi_home / "hooks"
    plugin_root = tmp_path / "plugin"
    plugin_hooks = plugin_root / "hooks"
    plugin_data = navi_home / "plugin-data" / "example"
    workspace.mkdir()
    hooks_dir.mkdir(parents=True)
    plugin_hooks.mkdir(parents=True)
    plugin_data.mkdir(parents=True)
    output = tmp_path / "order.txt"

    for path, label in (
        (navi_home / "config-hook", "config"),
        (hooks_dir / "file-hook", "file"),
        (hooks_dir / "hidden-hook", "hidden"),
        (plugin_hooks / "plugin-hook", "plugin"),
    ):
        path.write_text(
            f"#!/bin/sh\nprintf '{label}\\n' >> \"$ORDER_FILE\"\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    config_hooks = {
        "SessionStart": [
            {
                "matcher": "Bash",
                "hooks": [
                    {
                        "type": "command",
                        "command": "config-hook",
                        "env": {"ORDER_FILE": str(output)},
                    },
                    {
                        "type": "command",
                        "command": "config-hook",
                        "env": {"ORDER_FILE": str(output)},
                    },
                ]
            }
        ]
    }
    (hooks_dir / "10-file.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "file-hook",
                                    "env": {"ORDER_FILE": str(output)},
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (hooks_dir / "00-bad.json").write_text("{", encoding="utf-8")
    (hooks_dir / ".hidden.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "hidden-hook",
                                    "env": {"ORDER_FILE": str(output)},
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    manager = HookManager(
        workspace,
        navi_home,
        config_hooks,
        [
            {
                "plugin": "example",
                "config": {
                    "hooks": {
                        "SessionStart": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "plugin-hook",
                                        "env": {"ORDER_FILE": str(output)},
                                    }
                                ]
                            }
                        ]
                    }
                },
                "base": plugin_hooks,
                "root": plugin_root,
                "data_dir": plugin_data,
            }
        ],
    )

    assert manager.dispatch("SessionStart", "session-1", {}) is None
    assert output.read_text(encoding="utf-8").splitlines() == [
        "config",
        "file",
        "plugin",
    ]


@pytest.mark.skipif(os.name == "nt", reason="test script uses a POSIX shebang")
def test_command_receives_camel_case_envelope_and_reserved_environment(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    plugin_root = tmp_path / "plugin"
    plugin_data = navi_home / "plugin-data" / "example"
    plugin_hooks = plugin_root / "hooks"
    capture = tmp_path / "capture.json"
    workspace.mkdir()
    navi_home.mkdir()
    plugin_hooks.mkdir(parents=True)
    plugin_data.mkdir(parents=True)
    script = plugin_hooks / "capture"
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

Path(os.environ["CAPTURE"]).write_text(json.dumps({
    "input": json.load(sys.stdin),
    "event": os.environ["GROK_HOOK_EVENT"],
    "name": os.environ["GROK_HOOK_NAME"],
    "session": os.environ["GROK_SESSION_ID"],
    "workspace": os.environ["GROK_WORKSPACE_ROOT"],
    "naviEvent": os.environ["NAVI_HOOK_EVENT"],
    "naviName": os.environ["NAVI_HOOK_NAME"],
    "naviSession": os.environ["NAVI_SESSION_ID"],
    "naviWorkspace": os.environ["NAVI_WORKSPACE_ROOT"],
    "project": os.environ["CLAUDE_PROJECT_DIR"],
    "pluginRoot": os.environ["GROK_PLUGIN_ROOT"],
    "claudePluginRoot": os.environ["CLAUDE_PLUGIN_ROOT"],
    "pluginData": os.environ["GROK_PLUGIN_DATA"],
    "custom": os.environ["CUSTOM"],
}))
print('{"decision":"allow"}')
""",
        encoding="utf-8",
    )
    script.chmod(0o755)
    manager = HookManager(
        workspace,
        navi_home,
        {},
        [
            {
                "plugin": "example",
                "config": {
                    "hooks": {
                        "PreToolUse": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": "capture",
                                        "env": {
                                            "CAPTURE": str(capture),
                                            "CUSTOM": "kept",
                                            "GROK_HOOK_EVENT": "spoofed",
                                            "GROK_SESSION_ID": "spoofed",
                                            "GROK_PLUGIN_ROOT": "spoofed",
                                            "NAVI_HOOK_EVENT": "spoofed",
                                            "NAVI_SESSION_ID": "spoofed",
                                        },
                                    }
                                ]
                            }
                        ]
                    }
                },
                "base": plugin_hooks,
                "root": plugin_root,
                "data_dir": plugin_data,
            }
        ],
    )

    assert (
        manager.dispatch(
            "PreToolUse",
            "session-2",
            {
                "tool_name": "read_file",
                "tool_input": {"file_path": "README.md"},
                "tool_use_id": "call-1",
            },
        )
        is None
    )
    captured = json.loads(capture.read_text(encoding="utf-8"))
    assert captured["input"]["hookEventName"] == "pre_tool_use"
    assert captured["input"]["sessionId"] == "session-2"
    assert captured["input"]["toolName"] == "read_file"
    assert captured["input"]["toolInput"] == {"file_path": "README.md"}
    assert captured["input"]["toolUseId"] == "call-1"
    assert captured["input"]["workspaceRoot"] == str(workspace)
    assert captured["event"] == "pre_tool_use"
    assert captured["name"].startswith("plugin/example:")
    assert captured["session"] == "session-2"
    assert captured["workspace"] == str(workspace)
    assert captured["naviEvent"] == "pre_tool_use"
    assert captured["naviName"].startswith("plugin/example:")
    assert captured["naviSession"] == "session-2"
    assert captured["naviWorkspace"] == str(workspace)
    assert captured["project"] == str(workspace)
    assert captured["pluginRoot"] == str(plugin_root)
    assert captured["claudePluginRoot"] == str(plugin_root)
    assert captured["pluginData"] == str(plugin_data)
    assert captured["custom"] == "kept"


def test_matchers_use_exact_lists_regex_and_claude_aliases(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    output = tmp_path / "matched.txt"
    workspace.mkdir()
    navi_home.mkdir()
    exact_source = (
        f"from pathlib import Path; "
        f"Path({str(output)!r}).open('a').write('exact\\n')"
    )
    append_exact = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(exact_source)}"
    )
    regex_source = (
        f"from pathlib import Path; "
        f"Path({str(output)!r}).open('a').write('regex\\n')"
    )
    append_regex = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(regex_source)}"
    )
    manager = HookManager(
        workspace,
        navi_home,
        {
            "PostToolUse": [
                {
                    "matcher": "Bash|Read",
                    "hooks": [{"type": "command", "command": append_exact}],
                },
                {
                    "matcher": "^Bash$",
                    "hooks": [{"type": "command", "command": append_regex}],
                },
                {
                    "matcher": "[",
                    "hooks": [{"type": "command", "command": append_regex}],
                },
            ]
        },
    )

    manager.dispatch("PostToolUse", "session", {"tool_name": "bash"})
    manager.dispatch("PostToolUse", "session", {"tool_name": "bash_extra"})

    assert output.read_text(encoding="utf-8").splitlines() == ["exact", "regex"]


def test_legacy_tool_events_get_implicit_matchers(tmp_path):
    manager = HookManager(
        tmp_path,
        tmp_path / "home",
        {
            "beforeReadFile": [
                {"hooks": [{"type": "command", "command": "read-hook"}]}
            ],
            "beforeMCPExecution": [
                {"hooks": [{"type": "command", "command": "mcp-hook"}]}
            ],
            "afterFileEdit": [
                {"hooks": [{"type": "command", "command": "edit-hook"}]}
            ],
            "afterAgentResponse": [
                {"hooks": [{"type": "command", "command": "response-hook"}]}
            ],
        },
    )

    assert [handler["matcher"] for handler in manager.handlers["PreToolUse"]] == [
        "Read",
        "^mcp_",
    ]
    assert [handler["matcher"] for handler in manager.handlers["PostToolUse"]] == [
        "Edit|Write"
    ]


def test_pre_tool_use_json_decision_wins_and_first_deny_stops(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    marker = tmp_path / "later.txt"
    workspace.mkdir()
    navi_home.mkdir()
    allow_source = "import sys; print('{\"decision\":\"allow\"}'); sys.exit(2)"
    allow_with_exit_two = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(allow_source)}"
    )
    deny_source = (
        "import sys; "
        "print('{\"decision\":\"deny\",\"reason\":\"blocked\"}'); "
        "sys.exit(1)"
    )
    deny_with_exit_one = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(deny_source)}"
    )
    marker_source = (
        f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"
    )
    write_marker = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(marker_source)}"
    )
    manager = HookManager(
        workspace,
        navi_home,
        {
            "PreToolUse": [
                {
                    "matcher": "allow",
                    "hooks": [{"type": "command", "command": allow_with_exit_two}],
                },
                {
                    "matcher": "deny",
                    "hooks": [
                        {"type": "command", "command": deny_with_exit_one},
                        {"type": "command", "command": write_marker},
                    ],
                },
            ]
        },
    )

    assert manager.dispatch("PreToolUse", "session", {"tool_name": "allow"}) is None
    assert manager.dispatch("PreToolUse", "session", {"tool_name": "deny"}) == {
        "decision": "deny",
        "reason": "blocked",
    }
    assert not marker.exists()


@pytest.mark.parametrize(
    ("source", "expected_deny"),
    [
        ("import sys; sys.exit(0)", False),
        ("import sys; sys.exit(2)", True),
        ("import sys; sys.exit(1)", False),
        ("import sys; print('not-json'); sys.exit(2)", True),
        ("import sys; print('{\"decision\":\"later\"}'); sys.exit(2)", False),
    ],
)
def test_pre_tool_use_exit_and_invalid_output_semantics(
    tmp_path,
    source,
    expected_deny,
):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    workspace.mkdir()
    navi_home.mkdir()
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"
    manager = HookManager(
        workspace,
        navi_home,
        {
            "PreToolUse": [
                {"hooks": [{"type": "command", "command": command}]}
            ]
        },
    )

    result = manager.dispatch("PreToolUse", "session", {"tool_name": "bash"})

    assert (result is not None) is expected_deny


@pytest.mark.skipif(os.name == "nt", reason="process-group assertion is POSIX-specific")
def test_timeout_is_fail_open_kills_process_group_and_continues(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    pid_file = tmp_path / "timeout.pid"
    marker = tmp_path / "continued.txt"
    workspace.mkdir()
    navi_home.mkdir()
    slow = (
        f"echo $$ > {shlex.quote(str(pid_file))}; "
        "trap '' TERM; sleep 30 & wait"
    )
    continue_source = (
        f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')"
    )
    continue_command = (
        f"{shlex.quote(sys.executable)} -c "
        f"{shlex.quote(continue_source)}"
    )
    manager = HookManager(
        workspace,
        navi_home,
        {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": slow, "timeout": 0.1},
                        {"type": "command", "command": continue_command},
                    ]
                }
            ]
        },
    )

    assert manager.dispatch("PreToolUse", "session", {"tool_name": "bash"}) is None
    assert marker.read_text(encoding="utf-8") == "yes"
    with pytest.raises(ProcessLookupError):
        os.killpg(int(pid_file.read_text(encoding="utf-8")), 0)


@pytest.mark.skipif(os.name == "nt", reason="process-group assertion is POSIX-specific")
def test_scope_cancel_kills_process_group_and_raises_interrupt(tmp_path):
    workspace = tmp_path / "workspace"
    navi_home = tmp_path / "navi"
    pid_file = tmp_path / "cancel.pid"
    workspace.mkdir()
    navi_home.mkdir()
    command = (
        f"echo $$ > {shlex.quote(str(pid_file))}; "
        "trap '' TERM; sleep 30 & wait"
    )
    manager = HookManager(
        workspace,
        navi_home,
        {
            "PreToolUse": [
                {
                    "hooks": [
                        {"type": "command", "command": command, "timeout": 10}
                    ]
                }
            ]
        },
    )
    scope = TurnScope()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            manager.dispatch,
            "PreToolUse",
            "session",
            {"tool_name": "bash"},
            scope,
        )
        deadline = time.monotonic() + 3
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert pid_file.exists()
        process_group = int(pid_file.read_text(encoding="utf-8"))
        scope.cancel()
        with pytest.raises(KeyboardInterrupt):
            future.result(timeout=3)

    with pytest.raises(ProcessLookupError):
        os.killpg(process_group, 0)
