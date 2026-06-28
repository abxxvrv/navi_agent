from pathlib import Path
import json

from navi_agent.runtime.agent import AgentRuntime
from navi_agent.storage.history_store import HistoryStore
from navi_agent.tools.registry import ToolRegistry


class _SessionStore:
    meta = {}
    session_id = "test-session"


class _CommandTool:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _MemoryStore:
    def add(self, *_args, **_kwargs):
        return {"success": True}

    def replace(self, *_args, **_kwargs):
        return {"success": True}

    def remove(self, *_args, **_kwargs):
        return {"success": True}


def _runtime(tmp_path):
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.workspace = Path(tmp_path)
    runtime.tool_registry = ToolRegistry()
    runtime.memory_store = _MemoryStore()
    runtime.navi_home = Path(tmp_path) / "home"
    runtime.session_store = _SessionStore()
    runtime._channel = "cli"
    runtime.on_output = None
    runtime._pending_attachments = []
    return runtime


def test_register_tools_renames_run_command_to_bash(monkeypatch, tmp_path):
    monkeypatch.setattr("navi_agent.runtime.agent.RunCommandTool", _CommandTool)
    monkeypatch.setattr("navi_agent.runtime.agent.platform.system", lambda: "Linux")

    runtime = _runtime(tmp_path)
    runtime._register_tools()

    assert "bash" in runtime.tool_registry._tools
    assert "run_command" not in runtime.tool_registry._tools


def test_register_tools_only_exposes_powershell_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr("navi_agent.runtime.agent.RunCommandTool", _CommandTool)

    monkeypatch.setattr("navi_agent.runtime.agent.platform.system", lambda: "Linux")
    linux_runtime = _runtime(tmp_path / "linux")
    linux_runtime._register_tools()
    assert "powershell" not in linux_runtime.tool_registry._tools

    monkeypatch.setattr("navi_agent.runtime.agent.platform.system", lambda: "Windows")
    windows_runtime = _runtime(tmp_path / "windows")
    windows_runtime._register_tools()
    assert "powershell" in windows_runtime.tool_registry._tools


def test_memory_tool_rejects_invalid_target(monkeypatch, tmp_path):
    monkeypatch.setattr("navi_agent.runtime.agent.RunCommandTool", _CommandTool)
    monkeypatch.setattr("navi_agent.runtime.agent.platform.system", lambda: "Linux")

    runtime = _runtime(tmp_path)
    runtime._register_tools()

    result = runtime.tool_registry.invoke(
        "memory",
        {"action": "add", "target": "PROJECT", "content": "wrong target"},
    )

    assert result["success"] is False
    assert "未知记忆目标" in result["error"]


def test_resume_migrates_run_command_tool_name(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
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
                "mcp_servers": {},
            }
        ),
        encoding="utf-8",
    )
    for name in ("skills", "sessions", "memories", "agents", "logs"):
        (home / name).mkdir(exist_ok=True)
    monkeypatch.setenv("NAVI_HOME", str(home))

    old_session = HistoryStore(
        db_path=home / "history.sqlite3",
        project_path=tmp_path,
        provider="lmstudio",
        model="dummy",
    )
    old_session.append_message({"role": "system", "content": "Use run_command for tests"})
    old_session.set_tool_names(["read_file", "run_command"])

    runtime = AgentRuntime(workspace=tmp_path, resume_session_id=old_session.session_id)
    tool_names = {tool["function"]["name"] for tool in runtime._tools_for_api}

    assert "bash" in tool_names
    assert "run_command" not in tool_names
    assert "run_command" not in runtime._system_prompt
    assert "bash" in runtime._system_prompt
