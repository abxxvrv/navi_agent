from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .interrupt_scope import TurnScope


logger = logging.getLogger(__name__)


class HookManager:
    """Load compatible command hooks and dispatch runtime events."""

    def __init__(
        self,
        workspace: str | Path,
        navi_home: str | Path,
        config_hooks: dict[str, Any] | None = None,
        plugin_hooks: list[dict[str, Any]] | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        navi_home = Path(navi_home).resolve()
        self._event_names = {
            "SessionStart": "SessionStart",
            "session_start": "SessionStart",
            "sessionStart": "SessionStart",
            "UserPromptSubmit": "UserPromptSubmit",
            "user_prompt_submit": "UserPromptSubmit",
            "beforeSubmitPrompt": "UserPromptSubmit",
            "PreToolUse": "PreToolUse",
            "pre_tool_use": "PreToolUse",
            "preToolUse": "PreToolUse",
            "beforeShellExecution": "PreToolUse",
            "beforeMCPExecution": "PreToolUse",
            "beforeReadFile": "PreToolUse",
            "PostToolUse": "PostToolUse",
            "post_tool_use": "PostToolUse",
            "postToolUse": "PostToolUse",
            "afterShellExecution": "PostToolUse",
            "afterMCPExecution": "PostToolUse",
            "afterFileEdit": "PostToolUse",
            "afterAgentResponse": "PostToolUse",
            "afterAgentThought": "PostToolUse",
            "PostToolUseFailure": "PostToolUseFailure",
            "post_tool_use_failure": "PostToolUseFailure",
            "postToolUseFailure": "PostToolUseFailure",
            "PermissionDenied": "PermissionDenied",
            "permission_denied": "PermissionDenied",
            "permissionDenied": "PermissionDenied",
            "Stop": "Stop",
            "stop": "Stop",
            "StopFailure": "StopFailure",
            "stop_failure": "StopFailure",
            "stopFailure": "StopFailure",
            "SubagentStart": "SubagentStart",
            "subagent_start": "SubagentStart",
            "subagentStart": "SubagentStart",
            "SubagentStop": "SubagentStop",
            "subagent_stop": "SubagentStop",
            "subagentStop": "SubagentStop",
            "SubagentEnd": "SubagentStop",
            "subagent_end": "SubagentStop",
            "subagentEnd": "SubagentStop",
            "PreCompact": "PreCompact",
            "pre_compact": "PreCompact",
            "preCompact": "PreCompact",
            "PostCompact": "PostCompact",
            "post_compact": "PostCompact",
            "postCompact": "PostCompact",
            "SessionEnd": "SessionEnd",
            "session_end": "SessionEnd",
            "sessionEnd": "SessionEnd",
        }
        self._event_wire = {
            "SessionStart": "session_start",
            "UserPromptSubmit": "user_prompt_submit",
            "PreToolUse": "pre_tool_use",
            "PostToolUse": "post_tool_use",
            "PostToolUseFailure": "post_tool_use_failure",
            "PermissionDenied": "permission_denied",
            "Stop": "stop",
            "StopFailure": "stop_failure",
            "SubagentStart": "subagent_start",
            "SubagentStop": "subagent_stop",
            "PreCompact": "pre_compact",
            "PostCompact": "post_compact",
            "SessionEnd": "session_end",
        }
        self.handlers: dict[str, list[dict[str, Any]]] = {}

        sources: list[tuple[str, dict[str, Any], Path, dict[str, str]]] = []
        if isinstance(config_hooks, dict):
            sources.append(("config", config_hooks, navi_home, {}))
        hooks_dir = navi_home / "hooks"
        if hooks_dir.is_dir():
            for path in sorted(hooks_dir.glob("*.json")):
                if path.name.startswith(".") or path.name.endswith(
                    (".tmp.json", ".temp.json")
                ):
                    continue
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.warning("Invalid hook config %s: %s", path, exc)
                    continue
                if isinstance(value, dict):
                    sources.append((path.stem, value, path.parent, {}))
        for plugin in plugin_hooks or []:
            value = plugin.get("config")
            if not isinstance(value, dict):
                continue
            plugin_env: dict[str, str] = {}
            root = plugin.get("root")
            data_dir = plugin.get("data_dir")
            if root is not None:
                plugin_env["GROK_PLUGIN_ROOT"] = str(root)
                plugin_env["CLAUDE_PLUGIN_ROOT"] = str(root)
            if data_dir is not None:
                plugin_env["GROK_PLUGIN_DATA"] = str(data_dir)
                plugin_env["CLAUDE_PLUGIN_DATA"] = str(data_dir)
            sources.append(
                (
                    f"plugin/{plugin.get('plugin', 'unknown')}",
                    value,
                    Path(plugin.get("base") or root or self.workspace).resolve(),
                    plugin_env,
                )
            )

        seen_handlers: set[tuple[str, str, str]] = set()
        for source_name, value, base, source_env in sources:
            hooks = value.get("hooks", value)
            if not isinstance(hooks, dict):
                continue
            for event_name, groups in hooks.items():
                event = self._event_names.get(event_name)
                if event is None or not isinstance(groups, list):
                    continue
                for group_index, group in enumerate(groups):
                    if not isinstance(group, dict):
                        continue
                    matcher = group.get("matcher", "")
                    if not isinstance(matcher, str):
                        continue
                    matcher_regex = None
                    if matcher not in {"", "*"} and re.fullmatch(
                        r"[A-Za-z0-9_|]+",
                        matcher,
                    ) is None:
                        try:
                            matcher_regex = re.compile(matcher)
                        except re.error:
                            logger.warning(
                                "Invalid hook matcher %r in %s",
                                matcher,
                                source_name,
                            )
                            continue
                    entries = group.get("hooks")
                    if not isinstance(entries, list):
                        continue
                    for hook_index, entry in enumerate(entries):
                        if (
                            not isinstance(entry, dict)
                            or entry.get("type") != "command"
                            or not isinstance(entry.get("command"), str)
                            or not entry["command"]
                        ):
                            continue
                        identity = (event, entry["command"], matcher)
                        if identity in seen_handlers:
                            continue
                        seen_handlers.add(identity)
                        timeout = entry.get("timeout", 5)
                        if not isinstance(timeout, (int, float)) or timeout <= 0:
                            timeout = 5
                        environment = entry.get("env", {})
                        if not isinstance(environment, dict):
                            environment = {}
                        environment = {
                            str(key): value
                            for key, value in environment.items()
                            if isinstance(value, str)
                        }
                        environment.update(source_env)
                        self.handlers.setdefault(event, []).append(
                            {
                                "name": (
                                    f"{source_name}:{event}[{group_index}]"
                                    f".hooks[{hook_index}]"
                                ),
                                "command": entry["command"],
                                "base": base,
                                "timeout": float(timeout),
                                "environment": environment,
                                "matcher": matcher,
                                "matcher_regex": matcher_regex,
                            }
                        )

    def dispatch(
        self,
        event: str,
        session_id: str,
        payload: dict[str, Any],
        scope: TurnScope | None = None,
    ) -> dict[str, str] | None:
        event = self._event_names.get(event, event)
        if event not in self._event_wire:
            return None
        if scope is not None:
            scope.raise_if_cancelled()

        envelope: dict[str, Any] = {}
        for key, value in payload.items():
            parts = key.split("_")
            envelope[parts[0] + "".join(part.capitalize() for part in parts[1:])] = value
        envelope.update(
            {
                "hookEventName": self._event_wire[event],
                "sessionId": session_id,
                "cwd": str(self.workspace),
                "workspaceRoot": str(self.workspace),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        stdin = json.dumps(envelope, ensure_ascii=False)

        aliases = {
            "Bash": {"bash", "powershell"},
            "Read": {"read_file"},
            "Edit": {"patch_file"},
            "Write": {"write_file"},
            "MultiEdit": {"patch_file"},
            "Grep": {"grep"},
            "Glob": {"glob"},
            "ListDir": {"list_dir"},
            "WebSearch": {"web_search"},
            "WebFetch": {"web_extract"},
            "Task": {"agent"},
            "Agent": {"agent"},
        }
        for handler in self.handlers.get(event, []):
            matcher = handler["matcher"]
            if matcher not in {"", "*"} and event in {
                "PreToolUse",
                "PostToolUse",
                "PostToolUseFailure",
                "PermissionDenied",
            }:
                target = envelope.get("toolName")
                if not isinstance(target, str):
                    continue
                names = {target}
                for alias, tool_names in aliases.items():
                    if target in tool_names:
                        names.add(alias)
                if handler["matcher_regex"] is None:
                    if not any(name in names for name in matcher.split("|") if name):
                        continue
                elif not any(handler["matcher_regex"].search(name) for name in names):
                    continue

            command = handler["command"]
            if any(char in command for char in " \t\r\n|&;><$~"):
                if os.name == "nt":
                    argv = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", command]
                else:
                    argv = ["/bin/sh", "-c", command]
            else:
                executable = Path(command)
                if not executable.is_absolute():
                    executable = handler["base"] / executable
                argv = [str(executable)]

            environment = os.environ.copy()
            environment.update(handler["environment"])
            environment.update(
                {
                    "NAVI_HOOK_EVENT": self._event_wire[event],
                    "NAVI_HOOK_NAME": handler["name"],
                    "NAVI_SESSION_ID": session_id,
                    "NAVI_WORKSPACE_ROOT": str(self.workspace),
                    "GROK_HOOK_EVENT": self._event_wire[event],
                    "GROK_HOOK_NAME": handler["name"],
                    "GROK_SESSION_ID": session_id,
                    "GROK_WORKSPACE_ROOT": str(self.workspace),
                    "CLAUDE_PROJECT_DIR": str(self.workspace),
                }
            )
            popen_kwargs: dict[str, Any] = {
                "cwd": self.workspace,
                "env": environment,
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            }
            if os.name == "nt":
                popen_kwargs["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                )
            else:
                popen_kwargs["start_new_session"] = True
            try:
                process = subprocess.Popen(argv, **popen_kwargs)
            except OSError as exc:
                logger.warning("Hook %s failed to start: %s", handler["name"], exc)
                continue

            deadline = time.monotonic() + handler["timeout"]
            pending_input: str | None = stdin
            cancelled = False
            timed_out = False
            while True:
                try:
                    stdout, stderr = process.communicate(
                        pending_input,
                        timeout=max(0.001, min(0.05, deadline - time.monotonic())),
                    )
                    break
                except subprocess.TimeoutExpired:
                    pending_input = None
                    cancelled = scope is not None and scope.is_cancelled()
                    timed_out = time.monotonic() >= deadline
                    if cancelled or timed_out:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                timeout=2,
                                check=False,
                            )
                        else:
                            try:
                                os.killpg(process.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        stdout, stderr = process.communicate()
                        break

            if cancelled:
                assert scope is not None
                scope.raise_if_cancelled()
            if timed_out:
                logger.warning("Hook %s timed out", handler["name"])
                continue
            if event != "PreToolUse":
                if process.returncode:
                    logger.warning(
                        "Hook %s exited with status %s",
                        handler["name"],
                        process.returncode,
                    )
                continue

            output = stdout.strip()
            if output:
                try:
                    decision = json.loads(output)
                except json.JSONDecodeError:
                    decision = None
                if isinstance(decision, dict) and isinstance(
                    decision.get("decision"),
                    str,
                ):
                    if decision["decision"] == "deny":
                        reason = decision.get("reason")
                        return {
                            "decision": "deny",
                            "reason": (
                                reason
                                if isinstance(reason, str) and reason
                                else f"Denied by hook {handler['name']}"
                            ),
                        }
                    if decision["decision"] == "allow":
                        continue
                    continue
            if process.returncode == 2:
                return {
                    "decision": "deny",
                    "reason": f"Denied by hook {handler['name']} (exit code 2)",
                }
        return None
