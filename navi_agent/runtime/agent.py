from __future__ import annotations

import json
import logging
import random
import threading
import base64
from collections.abc import Callable
from contextlib import nullcontext
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
import os
import platform
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from ..paths import load_navi_dotenv

from ..context.context_manager import ContextManager
from ..integrations.lsp import LspManager
from ..storage.agent_store import AgentInstanceStore
from ..storage.history_store import HistoryStore
from .interrupt_scope import TurnScope
from .interruptible import run_model_stream, tool_worker, wait_approval
from .hooks import HookManager
from .monitor import Monitor
from .scheduler import Scheduler
from .task_manager import TaskManager
from .tool_context import CURRENT_TOOL_CONTEXT, ToolExecutionContext
from ..model.request import ModelStreamRunner
from ..model.router import ModelRouter
from ..paths import get_config_path, get_navi_home
from ..plugins import discover_plugins
from .interrupt import set_interrupt, is_interrupted
from ..tools.builtin import (
    GlobTool,
    GrepTool,
    ListDirTool,
    PatchTool,
    ReadFileTool,
    RunCommandTool,
    SkillViewTool,
    TavilyExtractTool,
    TavilySearchTool,
    VisionAnalyzeTool,
    WriteFileTool,
    SearchSessionTool,
    ReadSessionTool,
    resolve_path,
)
from ..tools.registry import ToolRegistry
from ..storage.version_tracker import VersionTracker
from ..context.compressor import ContextCompressor
from ..storage.memory_store import MemoryStore
from .background_review import BackgroundReviewer
from .goal import GOAL_TOOL_NAMES, GoalRunner
from .sub_agent import prepare_agent, EXPLORE_TOOLS
from ..skills.skill_manage import SkillManageTool
from ..storage.goal_store import GoalStore
from ..storage.scheduler_store import SchedulerStore

from ..tools.approval import (
    ApprovalDecision,
    ApprovalManager,
    UserApprovalChoice,
)

AgentEventHandler = Callable[[dict[str, Any]], None]

ApprovalHandler = Callable[[ApprovalDecision], str | UserApprovalChoice | bool]

BACKGROUND_TOOL_NAMES = {
    "get_task_output",
    "wait_tasks",
    "kill_task",
    "monitor",
    "scheduler_create",
    "scheduler_list",
    "scheduler_delete",
}


class EmptyModelResponseError(RuntimeError):
    pass


def get_final_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue
        if message.get("tool_calls"):
            continue

        content = message.get("content") or ""
        if not content.strip():
            continue

        return {
            "role": "assistant",
            "content": content,
        }

    return None


class AgentRuntime:
    def __init__(
        self,
        workspace: str | Path = ".",
        max_steps: int = 120,
        max_retries_per_step: int = 3,
        event_handler: AgentEventHandler | None = None,
        approval_mode: str = "normal",
        approval_handler: ApprovalHandler | None = None,
        resume_session_id: str | None = None,
        on_output=None,
        channel: str = "cli",
        enable_goal_mode: bool = True,
        plugin_dirs: list[Path] | None = None,
    ):
        load_navi_dotenv()

        self.workspace = Path(workspace).resolve()
        self.max_steps = max_steps
        self.max_retries_per_step = max(0, max_retries_per_step)
        self.event_handler = event_handler
        self.approval_handler = approval_handler
        self.on_output = on_output
        self._channel = channel
        self.enable_goal_mode = enable_goal_mode
        self.navi_home = get_navi_home()
        self.approval_manager = ApprovalManager(
            mode=approval_mode,
            workspace=self.workspace,
            navi_home=self.navi_home,
        )
        self._approval_lock = threading.Lock()

        history_db_path = self.navi_home / "history.sqlite3"

        # 读取全局默认模型配置（仅新会话参考，resume 时用会话自己的 meta）
        _config_path = get_config_path()
        _config = {}
        if _config_path.is_file():
            try:
                _config = json.loads(_config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        _default_provider = _config.get("default_provider") or _config.get("current_provider", "")
        _default_model = _config.get("default_model") or _config.get("current_model", "")

        # 判断是继续之前的会话还是新建会话
        if resume_session_id:
            self.session_store = HistoryStore.from_existing(history_db_path, resume_session_id)
            self.conversation_history = self._valid_messages(self.session_store.messages)
            # resume 时使用会话自己记录的模型
            _provider = self.session_store.meta.get("provider") or _default_provider
            _model = self.session_store.meta.get("model") or _default_model
        else:
            # 新会话使用默认模型
            _provider = _default_provider
            _model = _default_model
            self.session_store = HistoryStore(
                db_path=history_db_path,
                project_path=str(self.workspace),
                provider=_provider,
                model=_model,
                defer_persist=True,
                channel=channel,
            )
            self.conversation_history = []

        self.plugins = discover_plugins(
            self.workspace,
            self.navi_home,
            _config,
            plugin_dirs,
        )
        self.plugin_skills: dict[str, dict[str, Any]] = {}
        self.plugin_commands: dict[str, str] = {}
        self.plugin_agents: dict[str, dict[str, Any]] = {}
        self.plugin_mcp_servers: dict[str, Any] = {}
        self.plugin_lsp_servers: dict[str, Any] = {}
        self.plugin_hooks: list[dict[str, Any]] = []
        for plugin in self.plugins:
            if not plugin["enabled"]:
                continue
            for name, skill in plugin["skills"].items():
                self.plugin_skills.setdefault(
                    f"{plugin['name']}:{name}",
                    skill,
                )
            for name, command in plugin["commands"].items():
                self.plugin_commands.setdefault(f"{plugin['name']}:{name}", command)
            for name, agent in plugin["agents"].items():
                self.plugin_agents.setdefault(f"{plugin['name']}:{name}", agent)
            if plugin["trusted"]:
                for name, server in plugin["mcp_servers"].items():
                    self.plugin_mcp_servers.setdefault(name, server)
                for name, server in plugin["lsp_servers"].items():
                    self.plugin_lsp_servers.setdefault(name, server)
                if plugin["hooks"] is not None:
                    self.plugin_hooks.append(
                        {
                            "config": plugin["hooks"],
                            "base": plugin["hooks_base"],
                            "plugin": plugin["name"],
                            "root": plugin["root"],
                            "data_dir": plugin["data_dir"],
                        }
                    )

        self.mcp_servers = dict(_config.get("mcp_servers", {}))
        for name, server in self.plugin_mcp_servers.items():
            self.mcp_servers.setdefault(name, server)
        self.hooks = HookManager(
            self.workspace,
            self.navi_home,
            _config.get("hooks"),
            self.plugin_hooks,
        )
        self.lsp = LspManager(
            workspace=self.workspace,
            navi_home=self.navi_home,
            plugin_servers=self.plugin_lsp_servers,
        )

        self.tool_registry = ToolRegistry()
        self.memory_store = MemoryStore()
        self.context_manager = ContextManager(
            workspace=str(self.workspace),
            skills_path=str(self.navi_home / "skills"),
            navi_home=str(self.navi_home),
            memory_store=self.memory_store,
            plugin_skills=self.plugin_skills,
        )

        self.router = ModelRouter(_config_path, provider=_provider, model=_model)
        self.last_usage: dict[str, int] = self.session_store.get_usage()
        self.goal_runner = GoalRunner(self, GoalStore(history_db_path))
        if resume_session_id and self.enable_goal_mode:
            self.goal_runner.store.normalize_interrupted(self.session_store.session_id)
        self._goal_turn_reminder = ""

        # Ctrl+C 中断信号（流式循环中检查）
        self.cancel_event = threading.Event()
        self._pending_attachments: list[str] = []
        self._model_stream_runner = ModelStreamRunner(self.router, self.cancel_event)
        self._turn_lock = threading.Lock()
        self._current_scope: TurnScope | None = None
        # 当前执行线程 ID（用于线程级中断传播）
        self._execution_thread_id: int | None = None
        self._tool_worker_threads: set[int] = set()
        self._tool_worker_threads_lock = threading.Lock()
        self.agent_store = AgentInstanceStore(self.navi_home / "agents")
        self.task_manager = TaskManager(
            self.navi_home / "sessions" / self.session_store.session_id / "tasks",
            on_event=self._emit,
        )
        self.monitor = Monitor(self.task_manager, self._emit)
        self.scheduler = Scheduler(
            self.session_store.session_id,
            SchedulerStore(history_db_path),
            self._emit,
        )

        # 初始化后台审查器
        self.reviewer = BackgroundReviewer(
            router=self.router,
            tool_registry=self.tool_registry,
        )

        # 构建系统提示词，session 内固定不变
        # resume 时优先复用旧 session 的系统提示词，保持一致性
        persisted_system = self._get_persisted_system_message()
        if persisted_system is not None:
            self._system_prompt = persisted_system.replace("run_command", "bash")
        else:
            skill_index_prompt = self.context_manager.build_skill_index_prompt()
            _messages = self.context_manager.build_runtime_messages(
                messages=[],
                extra_instructions=skill_index_prompt,
            )
            self._system_prompt: str = _messages[0]["content"] if _messages else ""

        self._register_tools()
        if not self.enable_goal_mode:
            for name in GOAL_TOOL_NAMES:
                self.tool_registry.unregister(name)

        # 初始化 MCP 工具（如果配置了的话）
        self._init_mcp_tools()
        
        # 1. 把当前 registry 里所有已注册的工具转成 OpenAI API 的 tools 格式
        all_tools_for_api = self.tool_registry.to_openai_tools()
        
        # 2. 我们上一次保存的tool名字
        persisted_tool_names = self.session_store.meta.get("tool_names") or []
        persisted_tool_names = [
            "bash" if name == "run_command" else name
            for name in persisted_tool_names
        ]
        
        # 3. 如果是 resume 且旧 session 确实记录了工具列表
        if resume_session_id and persisted_tool_names:
            allowed = {*persisted_tool_names, *GOAL_TOOL_NAMES, *BACKGROUND_TOOL_NAMES}
            self._tools_for_api = [
                tool
                for tool in all_tools_for_api
                if tool.get("function", {}).get("name") in allowed
            ]
        else:
            self._tools_for_api = all_tools_for_api
            tool_names = [
                tool.get("function", {}).get("name", "")
                for tool in self._tools_for_api
            ]
            self.session_store.set_tool_names([name for name in tool_names if name])

        # 初始化上下文压缩器
        self.compressor = ContextCompressor(
            context_window=self.router.context_window,
            router=self.router,
        )
        self.hooks.dispatch(
            "SessionStart",
            self.session_store.session_id,
            {"source": "resume" if resume_session_id else "new"},
        )

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_handler is None:
            return

        try:
            self.event_handler(event)
        except Exception:
            pass

    def run_task(self, task: str) -> dict[str, Any]:
        with self._turn_lock:
            return self._invoke_agent(task, keep_history=False)

    def run_turn(self, user_input: str, image_paths: list[Path] | None = None) -> dict[str, Any]:
        with self._turn_lock:
            model_name = self.router.model_name
            result = self._invoke_agent(user_input, keep_history=True, image_paths=image_paths)
            result["model_name"] = model_name
            return result

    def interrupt(self, message: str | None = None) -> None:
        """Request the agent to interrupt its current loop.

        Call this from another thread (e.g., input handler)
        to gracefully stop the agent.

        Propagates the interrupt to the execution thread so that
        is_interrupted() checks inside tools and stream loops will see it.
        """
        scope = getattr(self, "_current_scope", None)
        if scope is not None:
            scope.cancel(message)
            return

        self.cancel_event.set()
        if self._execution_thread_id is not None:
            set_interrupt(True, self._execution_thread_id)
        with self._tool_worker_threads_lock:
            worker_thread_ids = list(self._tool_worker_threads)
        for thread_id in worker_thread_ids:
            set_interrupt(True, thread_id)
        model_stream_runner = getattr(self, "_model_stream_runner", None)
        if model_stream_runner is not None:
            model_stream_runner.abort()

    def _is_cancelled(self) -> bool:
        scope = getattr(self, "_current_scope", None)
        if scope is not None:
            return scope.is_cancelled()
        return self.cancel_event.is_set() or is_interrupted()

    def _raise_if_cancelled(self) -> None:
        scope = getattr(self, "_current_scope", None)
        if scope is not None:
            scope.raise_if_cancelled()
            return
        if self.cancel_event.is_set() or is_interrupted():
            raise KeyboardInterrupt("用户中断")

    def _require_scope(self) -> TurnScope:
        scope = getattr(self, "_current_scope", None)
        if scope is None:
            scope = TurnScope(self.cancel_event)
        return scope

    def list_tools(self) -> list[str]:
        return [name for name, spec in self.tool_registry._tools.items() if spec.visible]

    def get_model_info(self) -> dict[str, Any]:
        return {
            "current_provider": self.router.provider,
            "current_model": self.router.model,
            "current_model_name": self.router.model_name,
            "providers": self.router.list_providers(),
            "models": self.router.list_models(),
        }

    @property
    def is_busy(self) -> bool:
        return self._turn_lock.locked()

    def switch_model(self, provider_name: str, model_name: str) -> bool:
        if not self._turn_lock.acquire(blocking=False):
            return False
        try:
            ok = self.router.switch_model(provider_name, model_name)
            if ok:
                self.session_store.set_model(provider_name, model_name)
                config_path = get_config_path()
                config = json.loads(config_path.read_text(encoding="utf-8"))
                config["default_provider"] = provider_name
                config["default_model"] = model_name
                config_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            return ok
        finally:
            self._turn_lock.release()

    # 清洗历史会话。
    @staticmethod
    def _valid_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        safe_messages: list[dict[str, Any]] = []
        i = 0

        while i < len(messages):
            message = messages[i]
            role = message.get("role")
            if role == "system":
                i += 1
                continue
            if role == "tool":
                i += 1
                continue

            tool_calls = message.get("tool_calls") or []
            if role != "assistant" or not tool_calls:
                safe_messages.append(message)
                i += 1
                continue

            required_ids = [
                tool_call.get("id")
                for tool_call in tool_calls
                if (
                    isinstance(tool_call, dict)
                    and tool_call.get("id")
                    and (tool_call.get("function") or {}).get("name")
                )
            ]
            if len(required_ids) != len(tool_calls):
                i += 1
                continue

            tool_messages: list[dict[str, Any]] = []
            seen_ids: set[str] = set()
            j = i + 1
            is_valid = True
            while j < len(messages) and messages[j].get("role") == "tool":
                tool_call_id = messages[j].get("tool_call_id")
                if tool_call_id not in required_ids or tool_call_id in seen_ids:
                    is_valid = False
                    j += 1
                    break
                tool_messages.append(messages[j])
                seen_ids.add(tool_call_id)
                j += 1
                if len(seen_ids) == len(required_ids):
                    break

            if not is_valid or len(seen_ids) != len(required_ids):
                i = j
                continue

            safe_messages.append(message)
            safe_messages.extend(tool_messages)
            i = j

        return safe_messages

    def _build_image_history_text(self, user_input: str, image_paths: list[Path]) -> str:
        if not image_paths:
            return user_input
        lines: list[str] = ["用户发送了图片："]
        for path in image_paths:
            mime_type = VisionAnalyzeTool.MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
            lines.append(f"- {mime_type}: {path}")
        if user_input:
            lines.append("")
            lines.append("用户文本：")
            lines.append(user_input)
        return "\n".join(lines)

    def _vision_tool(self) -> VisionAnalyzeTool:
        spec = self.tool_registry._tools.get("vision_analyze")
        if spec is not None and isinstance(spec.function, VisionAnalyzeTool):
            return spec.function
        return VisionAnalyzeTool(
            workspace=self.workspace,
            config_path=get_config_path(),
            session_meta=self.session_store.meta,
        )

    def _build_image_api_user_message(self, user_input: str, image_paths: list[Path]) -> dict[str, Any]:
        if not image_paths:
            return {"role": "user", "content": user_input}

        model_info = (
            self.router.config
            .get("providers", {})
            .get(self.router.provider, {})
            .get("models", {})
            .get(self.router.model, {})
        )
        if bool(model_info.get("multimodal")):
            base_text = user_input or "请描述这张图片的内容"
            path_hints = "\n".join(f"[Image attached at: {path}]" for path in image_paths)
            api_text = f"{base_text}\n\n{path_hints}" if path_hints else base_text
            content: list[dict[str, Any]] = [{"type": "text", "text": api_text}]
            for path in image_paths:
                try:
                    raw = path.read_bytes()
                except Exception as exc:
                    content.append({"type": "text", "text": f"无法读取图片 {path}: 读取图片失败: {exc}"})
                    continue
                mime_type = VisionAnalyzeTool.MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
                b64 = base64.b64encode(raw).decode("utf-8")
                content.append({"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}})
            return {"role": "user", "content": content}

        parts = ["用户发送了图片："]
        for path in image_paths:
            mime_type = VisionAnalyzeTool.MIME_MAP.get(path.suffix.lower(), "application/octet-stream")
            parts.append(f"- {mime_type}: {path}")
        parts.extend(["", "图片内容："])
        vision_tool = self._vision_tool()
        for index, path in enumerate(image_paths, start=1):
            # 不传 prompt：视觉模型走内置默认提示词（~/.navi/vision_prompt.txt），
            # 用户文本留给主模型，不污染视觉模型的客观描述。
            result = vision_tool(image_path=str(path))
            if result.get("ok"):
                parts.append(f"[Image #{index}]")
                parts.append(str(result.get("content", "")))
            else:
                parts.append(f"[Image #{index}]")
                parts.append(f"无法分析图片：{result.get('error', 'unknown error')}")
            parts.append("")
        if user_input:
            parts.append("用户文本：")
            parts.append(user_input)
        return {"role": "user", "content": "\n".join(parts).strip()}

    # 调用agent，临时任务、对话模式都会用这个
    def _invoke_agent(
        self,
        user_input: str,
        keep_history: bool,
        image_paths: list[Path] | None = None,
    ) -> dict[str, Any]:
        scope = TurnScope(self.cancel_event)
        scope.reset()
        scope.attach_execution_thread()
        self._current_scope = scope
        self._execution_thread_id = scope.execution_thread_id
        with self._tool_worker_threads_lock:
            self._tool_worker_threads.clear()

        self._pending_attachments = []
        try:
            # 1. 清理和校验用户输入
            user_input = user_input.encode("utf-8", "replace").decode("utf-8").strip()
            image_paths = [Path(path) for path in (image_paths or [])]
            if not user_input and not image_paths:
                return {
                    "ok": False,
                    "error": "user_input 不能为空。",
                    "final_answer": "",
                }
            # 2. 准备上下文历史
            history = self.conversation_history if keep_history else []

            # 3. 构造当前用户消息
            history_text = self._build_image_history_text(user_input, image_paths)
            api_user_message = self._build_image_api_user_message(user_input, image_paths)
            # 模型看到的若是纯文本（含辅助视觉转出的“图片内容”），就原样落盘，
            # 让历史 == 模型实际看到的；多模态那版 content 是 base64 图片，仍只存轻量路径。
            if isinstance(api_user_message["content"], str):
                persisted_content = api_user_message["content"]
            else:
                persisted_content = history_text
            user_message = {
                "role": "user",
                "content": persisted_content,
            }
            self._ensure_persisted_system_message()
            snapshot_len = len(self.session_store.messages)
            self.session_store.append_message(user_message)
            self.reviewer.user_message_count += 1
            self.hooks.dispatch(
                "UserPromptSubmit",
                self.session_store.session_id,
                {"prompt": user_input},
                scope,
            )

            # 4. 构造本轮初始消息
            turn_messages = [*history, api_user_message]
            self._goal_turn_reminder = (
                self.goal_runner.build_reminder()
                if keep_history and self.enable_goal_mode
                else ""
            )
            if self._goal_turn_reminder:
                turn_messages.append(
                    {"role": "user", "content": self._goal_turn_reminder}
                )

            # 5. 执行 Agent 循环
            try:
                current_turn_messages = self._run_agent_loop(turn_messages)
            except KeyboardInterrupt:
                turn_messages = self.session_store.messages[snapshot_len:]
                responded_ids = {
                    m["tool_call_id"]
                    for m in turn_messages
                    if m.get("role") == "tool" and m.get("tool_call_id")
                }
                for m in turn_messages:
                    if m.get("role") != "assistant":
                        continue
                    for tc in m.get("tool_calls") or []:
                        tc_id = tc.get("id")
                        if tc_id and tc_id not in responded_ids:
                            cancelled = {
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": json.dumps(
                                    {"ok": False, "error": "用户中断"},
                                    ensure_ascii=False,
                                ),
                            }
                            self.session_store.append_message(cancelled)

                if keep_history:
                    self.conversation_history = self._valid_messages(self.session_store.messages)
                # 中断的消息和被中断后补发的消息视为同一次用户交互
                self.reviewer.user_message_count -= 1
                self.hooks.dispatch(
                    "Stop",
                    self.session_store.session_id,
                    {"reason": "cancelled"},
                )
                raise
            # 6. Agent 循环异常处理
            except Exception as exc:
                if keep_history:
                    self.conversation_history = self._valid_messages(self.session_store.messages)
                self.hooks.dispatch(
                    "StopFailure",
                    self.session_store.session_id,
                    {"error": str(exc)},
                )
                self.hooks.dispatch(
                    "Stop",
                    self.session_store.session_id,
                    {"reason": "error"},
                )
                return {
                    "ok": False,
                    "error": str(exc),
                    "final_answer": "",
                }

            # 7. 提取最终回答
            final_message = get_final_assistant_message(current_turn_messages)

            if final_message is None:
                final_message = {
                    "role": "assistant",
                    "content": "",
                }

            final_answer = final_message.get("content", "")

            # 8. 更新 conversation_history
            if keep_history and final_answer:
                # Keep the API-facing multimodal message alive for subsequent
                # turns while the process is running. The session store still
                # contains the lightweight path-only user message written
                # above, so persisted history remains free of base64 payloads.
                history_messages = current_turn_messages
                if self._goal_turn_reminder:
                    history_messages = [
                        message
                        for message in history_messages
                        if not (
                            message.get("role") == "user"
                            and message.get("content") == self._goal_turn_reminder
                        )
                    ]
                self.conversation_history = self._valid_messages(history_messages)

            self.hooks.dispatch(
                "Stop",
                self.session_store.session_id,
                {"reason": "end_turn"},
            )
            return { # 返回CLI
                "ok": bool(final_answer),
                "final_answer": final_answer,
                "content": final_answer,
                "error": None if final_answer else "本轮没有得到有效最终回复。", # 用于给CLI判断是否正确的
                "messages": current_turn_messages,
                "session_id": self.session_store.session_id,
                "session_path": str(self.session_store.path),
                "pending_attachments": list(self._pending_attachments),
            }
        finally:
            self._goal_turn_reminder = ""
            scope.close()
            if self._current_scope is scope:
                self._current_scope = None
            self._execution_thread_id = None

    def _get_persisted_system_message(self) -> str | None:
        for message in self.session_store.messages:
            if message.get("role") == "system":
                return message.get("content", "")
        return None

    def _ensure_persisted_system_message(self) -> None:
        if any(message.get("role") == "system" for message in self.session_store.messages):
            return

        if self._system_prompt:
            self.session_store.append_message({"role": "system", "content": self._system_prompt})

    def compress_context_to_new_session(self, *, reason: str = "manual") -> dict[str, Any]:
        old_store = self.session_store
        old_session_id = old_store.session_id
        old_messages = list(old_store.messages)
        self.hooks.dispatch(
            "PreCompact",
            old_session_id,
            {"source": reason, "messageCount": len(old_messages)},
            self._current_scope,
        )

        try:
            compressed_messages = self.compressor.compress(
                old_messages,
                messages_path=None,
            )
        except Exception as exc:
            return {
                "ok": False,
                "compressed": False,
                "reason": reason,
                "error": str(exc),
                "old_session_id": old_session_id,
                "new_session_id": old_session_id,
                "old_message_count": len(old_messages),
                "new_message_count": len(old_messages),
            }

        if compressed_messages == old_messages:
            return {
                "ok": True,
                "compressed": False,
                "reason": reason,
                "old_session_id": old_session_id,
                "new_session_id": old_session_id,
                "old_message_count": len(old_messages),
                "new_message_count": len(old_messages),
            }

        new_store = old_store.fork_with_messages(
            compressed_messages,
            title=old_store.meta.get("title"),
        )
        new_store.save_usage({})
        self.scheduler.rebind(new_store.session_id)
        for meta in self.agent_store.list_instances():
            if meta.get("parent_session_id") == old_session_id:
                self.agent_store.update_meta(
                    meta["agent_id"],
                    parent_session_id=new_store.session_id,
                )
        self.task_manager.log_dir = (
            self.navi_home / "sessions" / new_store.session_id / "tasks"
        )
        self.task_manager.log_dir.mkdir(parents=True, exist_ok=True)
        self.session_store = new_store
        self.conversation_history = self._valid_messages(new_store.messages)
        self.last_usage = {}
        self.goal_runner.rebind(old_session_id, new_store.session_id)
        self.hooks.dispatch(
            "PostCompact",
            new_store.session_id,
            {
                "source": reason,
                "oldSessionId": old_session_id,
                "newSessionId": new_store.session_id,
                "messageCount": len(new_store.messages),
            },
            self._current_scope,
        )

        return {
            "ok": True,
            "compressed": True,
            "reason": reason,
            "old_session_id": old_session_id,
            "new_session_id": new_store.session_id,
            "old_message_count": len(old_messages),
            "new_message_count": len(new_store.messages),
        }

    def _run_agent_loop(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """运行单 Agent 的模型/工具循环。"""
        steps = 0
        while True:
            self._raise_if_cancelled()
            if steps >= self.max_steps:
                raise RuntimeError(f"Agent 已达到最大执行步数（{self.max_steps}）。")
            messages = self._llm_node(messages)
            steps += 1
            self._raise_if_cancelled()

            if not messages[-1].get("tool_calls"):
                return messages

            if steps >= self.max_steps:
                raise RuntimeError(f"Agent 已达到最大执行步数（{self.max_steps}）。")
            messages = self._tool_node(messages)
            steps += 1
            self._raise_if_cancelled()
            budget_error = self.goal_runner.enforce_token_budget_after_step()
            if budget_error:
                raise RuntimeError(budget_error)

    # 构造真正发给模型的 messages
    def _llm_node(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        current_api_user_message = None
        if messages and messages[-1].get("role") == "user":
            if messages[-1].get("content") == self._goal_turn_reminder and len(messages) > 1:
                if messages[-2].get("role") == "user":
                    current_api_user_message = messages[-2]
            else:
                current_api_user_message = messages[-1]

        # 压缩检查
        prompt_tokens = self.last_usage.get("prompt_tokens", 0)
        if self.compressor.should_compress(prompt_tokens):
            result = self.compress_context_to_new_session(reason="auto")
            if result.get("ok") and result.get("compressed"):
                messages = self._valid_messages(self.session_store.messages)
                if current_api_user_message is not None and messages and messages[-1].get("role") == "user":
                    messages = [*messages[:-1], current_api_user_message]
                if self._goal_turn_reminder:
                    messages.append(
                        {"role": "user", "content": self._goal_turn_reminder}
                    )
            elif not result.get("ok"):
                self._emit(
                    {
                        "type": "compress_error",
                        "message": f"上下文压缩失败，跳过: {result.get('error', 'unknown error')}",
                    }
                )

        model_messages = [
            {"role": "system", "content": self._system_prompt},
            *messages,
        ]

        # 检查是否需要记忆反思
        if self.reviewer.user_message_count > 0 and self.reviewer.user_message_count % 10 == 0:
            self.reviewer.spawn_review(model_messages, "memory")

        starts_after_tool = bool(messages) and messages[-1].get("role") == "tool"

        for retry_count in range(self.max_retries_per_step + 1):
            content_parts: list[str] = []
            tool_calls_map: dict[int, dict] = {}  # index -> {id, function: {name, arguments}}
            reasoning_parts: list[str] = []
            displayed_reasoning = False

            try:
                self._raise_if_cancelled()

                # 流式调用模型
                stream = run_model_stream(
                    self._require_scope(),
                    self._model_stream_runner,
                    messages=model_messages,
                    tools=self._tools_for_api,
                )

                for chunk in stream:
                    # Ctrl+C 中断检查（两种机制都检查）
                    self._raise_if_cancelled()

                    # usage（最后一个 chunk，choices 通常为空）
                    if hasattr(chunk, "usage") and chunk.usage:
                        request_tokens = (
                            (chunk.usage.prompt_tokens or 0)
                            + (chunk.usage.completion_tokens or 0)
                        )
                        # prompt_tokens 只取最后一次（当前上下文大小）
                        self.last_usage["prompt_tokens"] = chunk.usage.prompt_tokens or 0
                        # completion_tokens 累加（全程生成总量）
                        self.last_usage["completion_tokens"] = self.last_usage.get("completion_tokens", 0) + (chunk.usage.completion_tokens or 0)
                        self.session_store.save_usage(self.last_usage)
                        self.goal_runner.record_tokens(request_tokens)

                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta

                    # 文本 token
                    if delta.content:
                        if displayed_reasoning and not content_parts:
                            self._emit({"type": "reasoning_end"})
                        content_parts.append(delta.content)
                        self._emit(
                            {
                                "type": "assistant_delta",
                                "content": delta.content,
                            }
                        )

                    # reasoning token（思考过程）
                    reasoning_content = getattr(delta, "reasoning_content", None)
                    if reasoning_content:
                        reasoning_parts.append(reasoning_content)
                        if not content_parts:
                            if not displayed_reasoning:
                                self._emit(
                                    {
                                        "type": "reasoning_start",
                                        "starts_after_tool": starts_after_tool,
                                    }
                                )
                            displayed_reasoning = True
                            self._emit(
                                {
                                    "type": "reasoning_delta",
                                    "content": reasoning_content,
                                }
                            )

                    # tool call
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_map:
                                tool_calls_map[idx] = {
                                    "id": "",
                                    "type": "function",
                                    "function": {"name": "", "arguments": ""},
                                }
                            if tc.id:
                                tool_calls_map[idx]["id"] += tc.id
                            if tc.function:
                                if tc.function.name:
                                    tool_calls_map[idx]["function"]["name"] += tc.function.name
                                if tc.function.arguments:
                                    tool_calls_map[idx]["function"]["arguments"] += tc.function.arguments

                # 拼接完整 message
                content = "".join(content_parts)
                reasoning_content = "".join(reasoning_parts)

                # 用户中断 → 保存已有内容并退出
                if self._is_cancelled():
                    if displayed_reasoning:
                        self._emit({"type": "reasoning_end"})
                    if content_parts:
                        self._emit({"type": "assistant_end"})
                    raise KeyboardInterrupt("用户中断")

                valid_tool_calls = [
                    tool_calls_map[i]
                    for i in sorted(tool_calls_map.keys())
                    if tool_calls_map[i].get("id")
                    and (tool_calls_map[i].get("function") or {}).get("name")
                ]

                if not content.strip() and not valid_tool_calls:
                    raise EmptyModelResponseError("模型没有返回正文或工具调用")
                if displayed_reasoning:
                    self._emit({"type": "reasoning_end"})
                if content_parts:
                    self._emit({"type": "assistant_end"})

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                }

                if reasoning_parts:
                    assistant_message["reasoning_content"] = reasoning_content

                if valid_tool_calls:
                    if content:
                        self._emit({"type": "assistant_content", "content": content})
                    assistant_message["tool_calls"] = valid_tool_calls

                self.session_store.append_message(assistant_message)

                return [*messages, assistant_message]
            except Exception as exc:
                if retry_count >= self.max_retries_per_step:
                    raise RuntimeError(
                        f"模型请求失败，已尝试 {self.max_retries_per_step + 1} 次：{exc}"
                    ) from exc

                delay = min(5.0, 0.3 * (2 ** retry_count)) + random.uniform(0, 0.5)
                self._emit(
                    {
                        "type": "retry",
                        "message": (
                            "模型响应异常，未保存本次未完成输出，"
                            f"正在重试 {retry_count + 1}/{self.max_retries_per_step}，"
                            f"等待 {delay:.1f}s：{exc}"
                        ),
                    }
                )
                time.sleep(delay)

        raise RuntimeError("模型请求失败。")

    def _execute_single_tool(
        self, tool_name: str, tool_args: dict, tool_call_id: str
    ) -> tuple[str, Any, str, dict]:
        """执行单个工具，供线程池调用。返回 (tool_call_id, result, name, args)。"""
        def invoke_tool() -> tuple[str, Any, str, dict]:
            self._emit({
                "type": "tool_start",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "tool_args": tool_args,
            })

            t0 = time.monotonic()
            token = CURRENT_TOOL_CONTEXT.set(
                ToolExecutionContext(
                    scope=scope,
                    tool_call_id=tool_call_id,
                )
            )
            try:
                tool_result = self.tool_registry.invoke(tool_name, tool_args)
            except Exception as exc:
                tool_result = {"ok": False, "error": str(exc)}
                self._emit({"type": "tool_error", "tool_call_id": tool_call_id,
                            "tool_name": tool_name, "tool_args": tool_args,
                            "error": str(exc)})
            else:
                elapsed = time.monotonic() - t0
                self._emit({"type": "tool_result", "tool_call_id": tool_call_id,
                            "tool_name": tool_name, "tool_args": tool_args,
                            "tool_result": tool_result, "elapsed": elapsed})
            finally:
                CURRENT_TOOL_CONTEXT.reset(token)
            elapsed = time.monotonic() - t0
            hook_payload = {
                "toolName": tool_name,
                "toolUseId": tool_call_id,
                "toolInput": tool_args,
                "durationMs": round(elapsed * 1000),
            }
            failed = isinstance(tool_result, dict) and tool_result.get("ok") is False
            if failed:
                hook_payload["error"] = str(
                    tool_result.get("error") or "Tool returned ok=false"
                )
            else:
                hook_payload["toolResult"] = tool_result
            self.hooks.dispatch(
                "PostToolUseFailure" if failed else "PostToolUse",
                self.session_store.session_id,
                hook_payload,
                scope,
            )
            return (tool_call_id, tool_result, tool_name, tool_args)

        scope = getattr(self, "_current_scope", None)
        if scope is not None:
            with tool_worker(scope):
                return invoke_tool()

        worker_thread_id = threading.get_ident()
        with self._tool_worker_threads_lock:
            self._tool_worker_threads.add(worker_thread_id)
        if self.cancel_event.is_set():
            set_interrupt(True, worker_thread_id)

        try:
            return invoke_tool()
        finally:
            with self._tool_worker_threads_lock:
                self._tool_worker_threads.discard(worker_thread_id)
            set_interrupt(False, worker_thread_id)

    def _execute_single_tool_ordered(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        wait_event: threading.Event | None,
        done_event: threading.Event | None,
    ) -> tuple[str, Any, str, dict]:
        """门闩包装：等前序同文件写入完成后再执行，自身完成后通知后续。"""
        try:
            if wait_event is not None:
                while not wait_event.wait(timeout=0.5):
                    if self._is_cancelled():
                        return (
                            tool_call_id,
                            {"ok": False, "error": "因前序同文件写入未完成或被中断而跳过"},
                            tool_name,
                            tool_args,
                        )
            return self._execute_single_tool(tool_name, tool_args, tool_call_id)
        finally:
            if done_event is not None:
                done_event.set()

    def _tool_node(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        last_message = messages[-1]
        failed_messages: list[dict] = []
        rejected_messages: list[dict] = []

        # 阶段一：解析参数
        parsed: list[tuple[str, str, dict]] = []  # (call_id, name, args)
        for tool_call in last_message.get("tool_calls", []):
            tool_name = tool_call["function"]["name"]
            try:
                tool_args = json.loads(tool_call["function"]["arguments"] or "{}")
                if not isinstance(tool_args, dict):
                    raise ValueError("工具参数必须是 JSON object。")
            except Exception as exc:
                tool_result = {"ok": False, "error": f"工具参数解析失败：{exc}"}
                self._emit({"type": "tool_error", "tool_name": tool_name,
                            "tool_args": {}, "error": str(exc)})
                tool_message = {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": json.dumps(tool_result, ensure_ascii=False),
                }
                self.session_store.append_message(tool_message)
                failed_messages.append(tool_message)
                continue
            parsed.append((tool_call["id"], tool_name, tool_args))

        # 阶段二：审批
        to_execute: list[tuple[str, str, dict]] = []  # (call_id, name, args)
        approval_batch_done = bool(parsed)
        try:
            for call_id, tool_name, tool_args in parsed:
                self._raise_if_cancelled()
                approval_result = self._handle_approval(
                    tool_call_id=call_id, tool_name=tool_name, tool_args=tool_args)
                self._raise_if_cancelled()
                if approval_result is not None:
                    tool_message = {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(approval_result, ensure_ascii=False),
                    }
                    self.session_store.append_message(tool_message)
                    rejected_messages.append(tool_message)
                    continue
                to_execute.append((call_id, tool_name, tool_args))
        finally:
            if approval_batch_done:
                self._emit({"type": "approval_batch_done"})

        # ── Pre-flight: 中断检查 ──
        self._raise_if_cancelled()

        # 收集多模态图片，稍后注入 user message
        pending_images: list[dict] = []

        # 阶段三：并发执行（同一真实路径的写操作按模型顺序串行，其余并行）
        executed_messages: list[dict] = []
        if to_execute:
            # 顺序门闩：把写同一真实路径的操作按模型顺序串成链，后一个等前一个完成
            wait_on: dict[str, threading.Event] = {}
            done_signal: dict[str, threading.Event] = {}
            last_writer: dict[str, str] = {}
            last_goal_tool: str | None = None
            for cid, name, args in to_execute:
                if name in GOAL_TOOL_NAMES:
                    if last_goal_tool is not None:
                        wait_on[cid] = done_signal.setdefault(
                            last_goal_tool, threading.Event()
                        )
                    last_goal_tool = cid
                if name not in ("write_file", "patch_file") or not args.get("path"):
                    continue
                try:
                    key = str(resolve_path(self.workspace, str(args["path"])))
                except Exception:
                    continue
                prev = last_writer.get(key)
                if prev is not None:
                    wait_on[cid] = done_signal.setdefault(prev, threading.Event())
                last_writer[key] = cid

            interrupted_during_tools = False
            executor = ThreadPoolExecutor(max_workers=len(to_execute))
            wait_for_workers = True
            try:
                futures = {
                    executor.submit(
                        self._execute_single_tool_ordered, name, args, cid,
                        wait_on.get(cid), done_signal.get(cid),
                    ): cid
                    for cid, name, args in to_execute
                }
                results: dict[str, tuple] = {}
                cancelled_call_ids: set[str] = set()

                # 轮询 future 完成情况，定期检查 interrupt
                pending = set(futures.keys())
                while pending:
                    done, pending = concurrent.futures.wait(pending, timeout=1.0)
                    for f in done:
                        call_id, result, name, args = f.result()
                        results[call_id] = (call_id, result, name, args)

                    # 中断检查：取消未完成的 future
                    if self._is_cancelled() and pending:
                        interrupted_during_tools = True
                        wait_for_workers = False
                        for f in pending:
                            f.cancel()
                        # 给正在跑的工具 3 秒优雅退出
                        done_after_cancel, still_pending = concurrent.futures.wait(pending, timeout=3.0)
                        for f in done_after_cancel:
                            cid = futures[f]
                            if f.cancelled():
                                cancelled_call_ids.add(cid)
                                tool_name = next(n for c, n, _ in to_execute if c == cid)
                                tool_message = {
                                    "role": "tool",
                                    "tool_call_id": cid,
                                    "content": json.dumps(
                                        {"ok": False, "error": f"工具执行已取消 — {tool_name} 因用户中断被跳过"},
                                        ensure_ascii=False,
                                    ),
                                }
                                self.session_store.append_message(tool_message)
                                failed_messages.append(tool_message)
                                continue
                            call_id, result, name, args = f.result()
                            results[call_id] = (call_id, result, name, args)
                        # 为被取消的 future 补上取消结果
                        for f in still_pending:
                            cid = futures[f]
                            cancelled_call_ids.add(cid)
                            tool_name = next(n for c, n, _ in to_execute if c == cid)
                            tool_message = {
                                "role": "tool",
                                "tool_call_id": cid,
                                "content": json.dumps(
                                    {"ok": False, "error": f"工具执行已取消 — {tool_name} 因用户中断被跳过"},
                                    ensure_ascii=False,
                                ),
                            }
                            self.session_store.append_message(tool_message)
                            failed_messages.append(tool_message)
                        break
            finally:
                executor.shutdown(wait=wait_for_workers, cancel_futures=True)

            if interrupted_during_tools or self._is_cancelled():
                raise KeyboardInterrupt("用户中断")

            for call_id, tool_name, tool_args in to_execute:
                if call_id in cancelled_call_ids or call_id not in results:
                    continue
                _, tool_result, _, _ = results[call_id]
                # 多模态工具结果：tool message 只放文字，图片收集起来
                if isinstance(tool_result, dict) and tool_result.get("_multimodal"):
                    image_data_url = tool_result.get("_image_data_url", "")
                    text_content = tool_result.get("content", "")
                    tool_content = json.dumps(
                        {
                            "ok": True,
                            "content": (
                                "Image loaded into context. "
                                "The image has been attached in the next user message — "
                                "you can see it directly. "
                                f"User request: {text_content}"
                            ),
                        },
                        ensure_ascii=False,
                    )
                    pending_images.append({
                        "prompt": text_content,
                        "image_data_url": image_data_url,
                    })
                else:
                    tool_content = json.dumps(tool_result, ensure_ascii=False)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": tool_content,
                }
                self.session_store.append_message(tool_message)
                executed_messages.append(tool_message)

        self._raise_if_cancelled()

        tool_messages = failed_messages + rejected_messages + executed_messages

        # 多模态图片注入：在 tool messages 之后插入 user message，让主模型看到图片
        if pending_images:
            user_content: list[dict] = []
            for img in pending_images:
                user_content.append({"type": "text", "text": img["prompt"]})
                user_content.append({
                    "type": "image_url",
                    "image_url": {"url": img["image_data_url"]},
                })
            injected_msg = {"role": "user", "content": user_content}
            self.session_store.append_message(injected_msg)
            tool_messages.append(injected_msg)

        # 更新工具调用轮次计数（并行的一批算一轮）
        if tool_messages:
            self.reviewer.tool_turn_count += 1

        # 检查是否需要技能反思
        if self.reviewer.tool_turn_count > 0 and self.reviewer.tool_turn_count % 15 == 0:
            self.reviewer.spawn_review(
                [
                    {"role": "system", "content": self._system_prompt},
                    *messages,
                    *tool_messages,
                ],
                "skill",
            )

        return [*messages, *tool_messages]

    # 在工具节点去审批的函数
    def _handle_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
        scope: TurnScope | None = None,
        hook_session_id: str | None = None,
    ) -> dict[str, Any] | None:
        hook_scope = scope or self._require_scope()
        session_id = hook_session_id or self.session_store.session_id
        hook_denial = self.hooks.dispatch(
            "PreToolUse",
            session_id,
            {
                "toolName": tool_name,
                "toolUseId": tool_call_id,
                "toolInput": tool_args,
            },
            hook_scope,
        )
        if hook_denial is not None:
            reason = hook_denial.get("reason") or "工具调用被 PreToolUse hook 拒绝。"
            self._emit(
                {
                    "type": "tool_error",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "error": reason,
                }
            )
            return {"ok": False, "error": reason, "hook": hook_denial}

        # 根据工具名字和参数，生成一个选择：看看是允许、拒绝、问用户的哪一种
        decision = self.approval_manager.check_tool_call(tool_name, tool_args)

        if decision.is_allow: # 如果被允许，返回空。
            return None

        if decision.is_deny: # 如果被拒绝，会返回调用失败的结果作为工具结果。
            tool_result = decision.to_tool_error()
            self.hooks.dispatch(
                "PermissionDenied",
                session_id,
                {
                    "toolName": tool_name,
                    "toolUseId": tool_call_id,
                    "toolInput": tool_args,
                    "reason": decision.reason,
                    "source": "policy",
                },
                hook_scope,
            )
            self._emit(
                {
                    "type": "tool_error",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "error": decision.reason,
                }
            )
            return tool_result

        try:
            with self._approval_lock:
                approval_scope = scope or self._require_scope()
                approval_scope.raise_if_cancelled()
                user_choice = wait_approval(
                    approval_scope,
                    self.approval_handler,
                    decision,
                )
                approval_scope.raise_if_cancelled()
                approved = self.approval_manager.resolve_user_choice(decision, user_choice) # 这是个 bool 值
        except Exception as exc:
            reason = f"审批处理失败：{exc}"
            tool_result = {
                "ok": False,
                "error": reason,
                "approval": {
                    "action": "ask",
                    "risk": decision.risk.value,
                    "tool_name": tool_name,
                    "command": decision.command,
                },
            }
            self._emit(
                {
                    "type": "tool_error",
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "error": reason,
                }
            )
            return tool_result

        if approved: # 如果用户同意执行，记录一下，返回 None
            return None

        # 走到这里说明没同意，返回没同意的结果
        reason = "用户拒绝执行该工具调用。"
        self.hooks.dispatch(
            "PermissionDenied",
            session_id,
            {
                "toolName": tool_name,
                "toolUseId": tool_call_id,
                "toolInput": tool_args,
                "reason": reason,
                "source": "user",
            },
            hook_scope,
        )
        tool_result = {
            "ok": False,
            "error": reason,
            "approval": {
                "action": "reject",
                "risk": decision.risk.value,
                "tool_name": tool_name,
                "command": decision.command,
            },
        }
        self._emit(
            {
                "type": "tool_error",
                "tool_name": tool_name,
                "tool_args": tool_args,
                "error": reason,
            }
        )
        return tool_result

    def _register_tools(self) -> None:
        workspace = str(self.workspace)
        tracker = VersionTracker()

        self.tool_registry.register(
            name="create_goal",
            description=(
                "Create a persistent goal only when the user explicitly asks for autonomous, "
                "multi-turn execution. Refuse if an active or resumable goal already exists; "
                "only the user-facing /goal replace command can replace a goal."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "objective": {"type": "string", "minLength": 1},
                    "completion_criterion": {"type": "string", "default": ""},
                },
                "required": ["objective"],
            },
            function=self.goal_runner.create_goal,
        )
        self.tool_registry.register(
            name="get_goal",
            description="Get the current goal, status, progress, and budgets.",
            parameters={"type": "object", "properties": {}},
            function=self.goal_runner.get_goal,
        )
        self.tool_registry.register(
            name="set_goal_budget",
            description=(
                "Set or replace one execution budget for the current goal. Repeated calls merge "
                "turn, token, and wall-clock budgets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "value": {"type": "number", "exclusiveMinimum": 0},
                    "unit": {
                        "type": "string",
                        "enum": [
                            "turns", "tokens", "milliseconds", "seconds", "minutes", "hours"
                        ],
                    },
                },
                "required": ["value", "unit"],
            },
            function=self.goal_runner.set_goal_budget,
        )
        self.tool_registry.register(
            name="update_goal",
            description=(
                "Update the current goal status. A normal final response never completes a goal; "
                "use complete only after checking the objective and completion criterion."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "complete", "paused", "blocked"],
                    }
                },
                "required": ["status"],
            },
            function=self.goal_runner.update_goal,
        )

        # list_dir
        self.tool_registry.register(
            name="list_dir",
            description="""
- 列出工作区内指定路径下的文件和目录。
""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "目录路径。默认相对于工作区；工作区外的目录请使用绝对路径。使用 '.' 表示工作区根目录。",
                        "default": ".",
                    },
                    "show_hidden": {
                        "type": "boolean",
                        "description": "是否包含隐藏文件和目录。",
                        "default": False,
                    },
                    "max_items": {
                        "type": "integer",
                        "description": "最多返回的项目数量。",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": [],
            },
            function=ListDirTool(workspace=workspace),
        )

        # read_file
        self.tool_registry.register(
            name="read_file",
            description="""
- 读取文本文件内容。只用于文本文件；无法读取图片、视频或其他二进制文件例如 .docx、.pdf。
- 如果读取到图片文件（PNG、JPG、GIF、WebP、BMP），会返回错误提示，请转用 vision_analyze 工具。
- 读取结果会像 cat -n 一样在每一行前加上行号。
- 默认且最多读取 1000 行，总返回内容最多 100KB；每一行内容最多保留 2000 字符。
- 不确定文件长度时，直接使用默认值 1000 行即可，不需要分多次读取。
- 本工具很适合并行调用，应该先并行调用 grep 确定要读内容，然后并行阅读。
- 单行超过 2000 字符会被截断并用 ... 标记，截断的行号会在 truncated_lines 中返回。
- 如果只需要文件的一部分，使用 start_line 和 max_lines。
- 如果要搜索内容或模式，优先使用 grep，而不是直接读取整文件。
- 配合 grep 使用时，将搜索结果中的 line 作为 start_line 直接跳转到匹配位置。
- 工具结果会返回 start_line、end_line、content、truncated 和 truncated_lines；如果读取失败会返回错误。
- truncated=false 表示本次读取没有因为 max_lines 或 max_chars 提前停止；如果 end_line 已覆盖目标行，应复用已有内容，不要重复读取。
- truncated=true 表示结果被行数或字符数限制截断，需要按 end_line 继续读取后续内容。
""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径。默认相对于工作区；工作区外的文件请使用绝对路径。",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "读取的起始行号，从 1 开始。",
                        "default": 1,
                        "minimum": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取的行数。默认 1000，最大 1000。",
                        "default": 1000,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "返回内容的最大字符数。默认 102400，范围 1000-102400。",
                        "default": 102400,
                        "minimum": 1000,
                        "maximum": 102400,
                    },
                },
                "required": ["path"],
            },
            function=ReadFileTool(workspace=workspace, tracker=tracker),
        )

        # write_file
        self.tool_registry.register(
            name="write_file",
            description="""
- 向文本文件写入内容。
- 如果文件不存在会创建文件；如果父目录不存在会自动创建。
- 写代码到文件时必须使用此工具，不要把回复里的代码当作实际写入。
- 修改已有文件时，尽可能优先使用 patch_file；只有创建新文件、追加内容或完整重写文件时才使用 write_file。
- 该工具会返回 changed、added_lines、removed_lines 和 diff，便于检查实际改动。
""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径。默认相对于工作区；工作区外的文件请使用绝对路径。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要写入文件的文本内容。",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["overwrite", "append"],
                        "description": "写入模式。",
                    },
                    "encoding": {
                        "type": "string",
                        "description": "文本编码，通常使用 'utf-8'。",
                    },
                },
                "required": ["path", "content"],
            },
            function=WriteFileTool(workspace=workspace, tracker=tracker),
        )

        # patch_file
        self.tool_registry.register(
            name="patch_file",
            description="""
- 在已有文本文件中替换特定字符串。
- 使用该工具对已有文件进行定向修改。
- old_text 必须精确匹配，包括空格和缩进。可以适当多一些内容以保证匹配唯一。
- 默认要求 old_text 在文件中唯一；如果出现多个匹配结果会失败，除非明确设置 replace_all=true。
- 如果是创建新文件、追加内容或完整重写文件，请使用 write_file。
- 该工具会返回 changed、replacements、added_lines、removed_lines 和 diff，便于检查实际改动。
""",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径。默认相对于工作区；工作区外的文件请使用绝对路径。",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "要查找的精确文本。",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "用于替换的新文本。",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项。",
                        "default": False,
                    },
                    "encoding": {
                        "type": "string",
                        "description": "文本编码。",
                        "default": "utf-8",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
            function=PatchTool(workspace=workspace, tracker=tracker),
        )

        # skill_view
        self.tool_registry.register(
            name="skill_view",
            description="""
- 本工具可以查看指定技能的 SKILL.md 内容。
- 当你要调用某个技能或者非常需要查看具体的技能文件时使用。
""",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能名称，例如 skill-creator、docx",
                    },
                },
                "required": ["name"],
            },
            function=SkillViewTool(
                workspace=workspace,
                skills_path=str(self.navi_home / "skills"),
                plugin_skills=self.plugin_skills,
                session_id=self.session_store.session_id,
            ),
        )

        # skill_manage
        self.tool_registry.register(
            name="skill_manage",
            description="""
- 管理技能文件：列出所有技能、查看某个技能的完整内容、创建/覆盖技能、定向修补技能，或删除技能目录。
- 只能在 skills/ 目录下操作。
- action="list"：列出所有可用技能（名称 + 简介）。
- action="read"：读取指定技能的完整 SKILL.md 内容，需要 name 参数。
- action="write"：创建或整体覆盖技能，需要 name 和 content 参数。
- action="patch"：在已有技能的 SKILL.md 中做定向文本替换，需要 name、old_text、new_text；old_text 必须精确匹配，默认要求唯一，多处匹配需设 replace_all=true。
- action="delete"：删除指定技能目录，需要 name 参数；目标目录必须包含 SKILL.md。
- content 必须是完整的 SKILL.md（含 YAML frontmatter）。
""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write", "patch", "delete"],
                        "description": "操作类型：list（列出技能）、read（读取）、write（创建/覆盖）、patch（定向替换）、delete（删除技能目录）",
                    },
                    "name": {
                        "type": "string",
                        "description": "技能名称，如 skill-creator。read / write / patch / delete 时必填。",
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的 SKILL.md 内容（含 YAML frontmatter），write 时必填。",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "要查找的精确文本，patch 时必填。",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "用于替换的新文本，patch 时必填。",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "是否替换所有匹配项，patch 时可选。",
                        "default": False,
                    },
                },
                "required": ["action"],
            },
            function=SkillManageTool(),
            visible=False,
        )

        bash_tool = RunCommandTool(
            workspace=workspace,
            task_manager=self.task_manager,
            on_output=self.on_output,
        )
        self.tool_registry.register(
            name="bash",
            description="""
- 可以运行短时间、非交互式的 Bash 命令。
- 写完代码后，使用该工具运行测试、检查语法或查看错误信息。
- 命令应使用 Bash 语法，不是 PowerShell 或 cmd 语法。
- 该工具会返回 stdout、stderr、output、exit_code、timed_out 和 shell。
- 长时间构建或服务命令使用 background=true；工具会立即返回 task_id。
- 对会修改工作区外系统内容的命令要谨慎，必要时使用明确路径。
""",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要运行的短时间、非交互式 Bash 命令。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "命令运行目录。默认相对于工作区；工作区外的目录请使用绝对路径。",
                        "default": ".",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            "命令超时时间，单位秒。前台默认 60、最多 300；"
                            "后台省略或设为 0 表示一直运行，正数最多 36000。"
                        ),
                        "minimum": 0,
                        "maximum": 36000,
                    },
                    "encoding": {
                        "type": "string",
                        "description": "输出解码编码。",
                        "default": "utf-8",
                    },
                    "background": {
                        "type": "boolean",
                        "description": "在后台运行并立即返回 task_id。",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
            function=bash_tool,
        )

        if platform.system() == "Windows":
            self.tool_registry.register(
                name="powershell",
                description="""
- 可以运行短时间、非交互式的 PowerShell 命令。
- 只在 Windows 环境可用；需要 PowerShell 语法时使用它，不要用 bash 硬凑。
- 写完 Windows/PowerShell 相关代码后，可用该工具运行测试、检查语法或查看错误信息。
- 该工具会返回 stdout、stderr、output、exit_code、timed_out 和 shell。
- 长时间构建或服务命令使用 background=true；工具会立即返回 task_id。
- 对会修改工作区外系统内容的命令要谨慎，必要时使用明确路径。
""",
                parameters={
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "要运行的短时间、非交互式 PowerShell 命令。",
                        },
                        "cwd": {
                            "type": "string",
                            "description": "命令运行目录。默认相对于工作区；工作区外的目录请使用绝对路径。",
                            "default": ".",
                        },
                        "timeout_seconds": {
                            "type": "integer",
                            "description": (
                                "命令超时时间，单位秒。前台默认 60、最多 300；"
                                "后台省略或设为 0 表示一直运行，正数最多 36000。"
                            ),
                            "minimum": 0,
                            "maximum": 36000,
                        },
                        "encoding": {
                            "type": "string",
                            "description": "输出解码编码。",
                            "default": "utf-8",
                        },
                        "background": {
                            "type": "boolean",
                            "description": "在后台运行并立即返回 task_id。",
                            "default": False,
                        },
                    },
                    "required": ["command"],
                },
                function=RunCommandTool(
                    workspace=workspace,
                    shell="powershell",
                    task_manager=self.task_manager,
                    on_output=self.on_output,
                ),
            )

        self.tool_registry.register(
            name="get_task_output",
            description=(
                "Get status and output for up to 20 commands, monitors, or subagents. Omit timeout_ms or "
                "use 0 to poll; a positive timeout waits until all requested tasks finish."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 600000,
                        "default": 0,
                    },
                },
                "required": ["task_ids"],
            },
            function=self.task_manager.get_output,
        )
        self.tool_registry.register(
            name="wait_tasks",
            description="Wait until any or all requested commands, monitors, or subagents finish.",
            parameters={
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 20,
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["wait_any", "wait_all"],
                        "default": "wait_all",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 600000,
                        "default": 30000,
                    },
                },
                "required": ["task_ids"],
            },
            function=self.task_manager.wait_tasks,
        )
        self.tool_registry.register(
            name="kill_task",
            description="Stop a command, monitor, or subagent by task ID.",
            parameters={
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
            function=self.task_manager.kill,
        )
        self.tool_registry.register(
            name="monitor",
            description=(
                "Start a background monitor. Each non-empty output line becomes a session "
                "event; keep the command selective and line-buffered."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "description": {"type": "string"},
                    "timeout_ms": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 36000000,
                        "default": 36000000,
                    },
                    "persistent": {"type": "boolean", "default": False},
                },
                "required": ["command", "description"],
            },
            function=lambda command, description, timeout_ms=None, persistent=False: self.monitor.start(
                command,
                description,
                workspace,
                bash_tool.shell_path,
                timeout_ms=timeout_ms,
                persistent=persistent,
            ),
        )
        self.tool_registry.register(
            name="scheduler_create",
            description=(
                "Schedule a prompt. Intervals use Ns, Nm, Nh, or Nd and are clamped to a "
                "minimum of 60 seconds. Recurring tasks expire after 7 days."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "interval": {"type": "string"},
                    "prompt": {"type": "string", "minLength": 1},
                    "recurring": {"type": "boolean", "default": True},
                    "durable": {"type": "boolean", "default": False},
                    "fire_immediately": {"type": "boolean", "default": False},
                },
                "required": ["interval", "prompt"],
            },
            function=self.scheduler.create,
        )
        self.tool_registry.register(
            name="scheduler_list",
            description="List active scheduled prompts and their next fire times.",
            parameters={"type": "object", "properties": {}},
            function=self.scheduler.list,
        )
        self.tool_registry.register(
            name="scheduler_delete",
            description="Cancel a scheduled prompt by ID.",
            parameters={
                "type": "object",
                "properties": {"id": {"type": "string"}},
                "required": ["id"],
            },
            function=self.scheduler.delete,
        )

        # glob
        self.tool_registry.register(
            name="glob",
            description="""
- 快速的文件模式匹配工具，适用于任意规模的代码库
- 支持 glob 模式，如 "src/**/*.ts"。从默认工作区根目录搜索时会拒绝 "**/*.py"，请写成更具体的 "navi_agent/**/*.py"，或指定 path: "navi_agent", pattern: "**/*.py"。
- 返回匹配的文件路径
- 当你需要按文件名模式查找文件时使用此工具
""",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "glob 匹配模式，如 '*.py'、'src/**/*.ts'。从默认工作区根目录搜索时不能以 ** 开头。",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索目录。默认相对于工作区；工作区外的目录请使用绝对路径。默认 '.'。",
                        "default": ".",
                    },
                    "include_dirs": {
                        "type": "boolean",
                        "description": "结果是否包含目录。默认 false。",
                        "default": False,
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "结果是否包含隐藏文件或隐藏目录。默认 false。",
                        "default": False,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回的匹配数量。默认 100，最大 1000。",
                        "default": 100,
                        "minimum": 1,
                        "maximum": 1000,
                    },
                },
                "required": ["pattern"],
            },
            function=GlobTool(workspace=workspace),
        )

        # grep
        self.tool_registry.register(
            name="grep",
            description="""
- 一个基于 ripgrep 构建的强大搜索工具
- 始终使用 grep 进行搜索任务。绝不要通过 Bash 命令调用 `grep` 或 `rg`。grep 工具已针对正确的权限和访问进行了优化。
- 支持完整的正则表达式语法（如 "log.*Error"、"function\\s+\\w+"）
- 使用 glob 参数（如 "*.js"、"**/*.tsx"）
- 输出模式："context_lines" 显示匹配行
""",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或正则表达式。不可为空。",
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索的起始目录，相对于工作区。默认 '.'。工作区外的文件请使用绝对路径。",
                        "default": ".",
                    },
                    "glob": {
                        "type": "string",
                        "description": "文件名过滤，支持 glob 模式，如 '*.py'、'*.js'。留空表示搜索所有文件。",
                        "default": "",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少条匹配。默认 30，最小 1，最大 100",
                        "default": 30,
                        "minimum": 1,
                        "maximum": 100,
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "匹配行前后各显示几行上下文。默认 3，最小 0，最大 5",
                        "default": 3,
                        "minimum": 0,
                        "maximum": 5,
                    },
                },
                "required": ["query"],
            },
            function=GrepTool(workspace=workspace),
        )

        if self.lsp.servers:
            self.tool_registry.register(
                name="lsp",
                description=(
                    "Query a configured language server for definitions, references, "
                    "implementations, document symbols, or workspace symbols. Input positions "
                    "are 0-based; returned positions are 1-based."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "operation": {
                            "type": "string",
                            "enum": [
                                "goToDefinition",
                                "findReferences",
                                "goToImplementation",
                                "documentSymbol",
                                "workspaceSymbol",
                            ],
                        },
                        "file_path": {
                            "type": "string",
                            "description": "Absolute path or path relative to the workspace.",
                        },
                        "line": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "0-based line for position queries.",
                        },
                        "character": {
                            "type": "integer",
                            "minimum": 0,
                            "description": "0-based character for position queries.",
                        },
                        "query": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Symbol name for workspaceSymbol.",
                        },
                    },
                    "required": ["operation"],
                },
                function=self.lsp.query,
            )

        # web_search
        self.tool_registry.register(
            name="web_search",
            description="""
- 搜索互联网，获取实时网页信息。
- 当你需要查找最新的技术文档、库的用法、新闻、或其他网络上才能找到的信息时使用。
- 返回结构化的搜索结果，包含标题、URL、内容摘要和 AI 生成的答案。
- 支持指定搜索深度（basic/advanced）、结果数量、域名过滤等。
""",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词。",
                    },
                    "search_depth": {
                        "type": "string",
                        "description": "搜索深度，默认'basic'。'basic' 快速搜索，'advanced' 深度搜索。",
                        "default": "basic",
                        "enum": ["basic", "advanced"],
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的结果数量，默认 5，最大 20。",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "include_answer": {
                        "type": "boolean",
                        "description": "是否返回 AI 生成的摘要回答，默认 true。",
                        "default": True,
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "限定搜索的域名列表，如 ['github.com', 'stackoverflow.com']。",
                    },
                    "exclude_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "排除的域名列表。",
                    },
                },
                "required": ["query"],
            },
            function=TavilySearchTool(),
        )

        # web_extract
        self.tool_registry.register(
            name="web_extract",
            description="""
- 提取指定 URL 的网页内容。
- 当你需要读取某个网页的具体内容时使用，返回 Clean Markdown 格式的正文。
- 支持 basic（快速）和 advanced（深度，会渲染 JS）两种提取深度。
""",
            parameters={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要提取内容的网页 URL。",
                    },
                    "extract_depth": {
                        "type": "string",
                        "description": "提取深度，默认 'basic'。'basic' 快速提取，'advanced' 深度提取（会执行 JS 渲染）。",
                        "default": "basic",
                        "enum": ["basic", "advanced"],
                    },
                },
                "required": ["url"],
            },
            function=TavilyExtractTool(),
        )

        # memory
        def memory_tool(action: str, target: str, content: str = None, old_text: str = None) -> dict:
            if target not in {"memory", "user"}:
                return {"success": False, "error": f"未知记忆目标 '{target}'"}
            if action == "add":
                if not content:
                    return {"success": False, "error": "add 操作需要 content 参数"}
                return self.memory_store.add(target, content)
            elif action == "replace":
                if not old_text or not content:
                    return {"success": False, "error": "replace 操作需要 old_text 和 content 参数"}
                return self.memory_store.replace(target, old_text, content)
            elif action == "remove":
                if not old_text:
                    return {"success": False, "error": "remove 操作需要 old_text 参数"}
                return self.memory_store.remove(target, old_text)
            else:
                return {"success": False, "error": f"未知操作 '{action}'"}

        self.tool_registry.register(
            name="memory",
            description="""
- 管理全局持久化记忆，跨会话保留。
- 存储目标 memory：你的全局笔记（跨项目环境事实、工具特性、通用经验教训）。
- 存储目标 user：用户画像（用户偏好、沟通风格、工作习惯、技术栈）。
- 当前项目的项目记忆不用本工具：按系统提示词里的项目记忆规范直接读写 .navi/memories/ 下的文件。
- action="add"：添加一条新记忆。
- action="replace"：更新已有记忆（用 old_text 定位，用 content 替换）。
- action="remove"：删除已有记忆（用 old_text 定位）。
- 何时保存：用户纠正你、用户分享偏好、你发现环境事实、你学到经验教训。
- 不要保存：任务进度、临时状态、容易重新发现的信息。
""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "replace", "remove"],
                        "description": "操作类型。add=添加新条目，replace=替换已有条目，remove=删除条目。",
                    },
                    "target": {
                        "type": "string",
                        "enum": ["memory", "user"],
                        "description": "存储目标。memory=你的笔记，user=用户画像。",
                    },
                    "content": {
                        "type": "string",
                        "description": "要添加或替换的内容。add 和 replace 时必填。",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "要查找的文本片段。必须是已有条目中的一个子串，用于定位要替换或删除的条目。replace 和 remove 时必填。",
                    },
                },
                "required": ["action", "target"],
            },
            function=memory_tool,
        )

        # vision_analyze
        self.tool_registry.register(
            name="vision_analyze",
            description="""
- 识别图片并返回文本内容。支持本地图片文件和图片 URL。
- 当用户提到图片、截图、照片，或 read_file 返回 hint="vision_analyze" 时，使用此工具分析图片。
- 支持格式：JPEG、PNG、GIF、WebP、BMP。
- 可选传入 prompt 指定对图片的提问，默认描述图片全部内容。
""",
            parameters={
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "图片路径（本地文件路径或公网可访问的图片 URL）。",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "对图片的提问或指令。可选；不传时使用内置默认提示词。",
                    },
                },
                "required": ["image_path"],
            },
            function=VisionAnalyzeTool(
                workspace=self.workspace,
                config_path=get_config_path(),
                session_meta=self.session_store.meta,
            ),
        )

        # search_session
        self.tool_registry.register(
            name="search_session",
            description=SearchSessionTool.description,
            parameters=SearchSessionTool.parameters,
            function=SearchSessionTool(
                navi_home=self.navi_home,
                current_session_id=self.session_store.session_id,
            ),
        )

        # read_session
        self.tool_registry.register(
            name="read_session",
            description=ReadSessionTool.description,
            parameters=ReadSessionTool.parameters,
            function=ReadSessionTool(
                navi_home=self.navi_home,
                current_session_id=self.session_store.session_id,
            ),
        )

        # agent：派生子 agent 处理独立子任务
        self.tool_registry.register(
            name="agent",
            description="""
- 派生子 agent 完成边界清晰的独立任务。默认后台运行并立即返回 task_id；之后用 get_task_output、wait_tasks 或 kill_task 管理。
- background=false 会等待结果；等待超过 timeout_ms 时任务会继续在后台运行，不会被取消。
- general-purpose 可读写并执行命令；explore 只读探索；plan 只读分析并输出实施计划。子 agent 不能继续派生子 agent。
- prompt 必须自包含；description 是用于任务列表和完成提醒的短标签。
- resume_from 会从同一父会话、同类型且已完成的子 agent 继续，并创建新的 subagent_id。
""",
            parameters={
                "type": "object",
                "properties": {
                    "subagent_type": {
                        "type": "string",
                        "enum": [
                            "general-purpose",
                            "explore",
                            "plan",
                            *sorted(self.plugin_agents),
                        ],
                        "default": "general-purpose",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "给子 agent 的完整、自包含的任务描述（作为 user 消息传入）。",
                        "minLength": 1,
                    },
                    "description": {
                        "type": "string",
                        "description": "任务短标签。",
                        "minLength": 1,
                    },
                    "background": {
                        "type": "boolean",
                        "description": "后台运行并立即返回 task_id。",
                        "default": True,
                    },
                    "model": {
                        "type": "object",
                        "description": "（可选）为子 agent 指定模型；不传则用主模型。",
                        "properties": {
                            "provider": {"type": "string", "description": "供应商名，如 deepseek / mimo。"},
                            "model": {"type": "string", "description": "模型名。"},
                        },
                        "required": ["provider", "model"],
                    },
                    "resume_from": {
                        "type": "string",
                        "description": "已完成子 agent 的 ID；类型和父会话必须相同。",
                    },
                    "timeout_ms": {
                        "type": "integer",
                        "description": "前台等待预算，超时后任务转后台。",
                        "default": 600000,
                        "minimum": 1,
                        "maximum": 600000,
                    },
                },
                "required": ["prompt", "description"],
            },
            function=self._run_subagent,
        )

        # attach_file 仅在具备附件回传能力的消息网关中注册。
        if self._channel in {"qq", "weixin"}:
            def attach_file(path: str) -> dict:
                p = Path(path)
                if not p.is_absolute():
                    p = (self.workspace / path).resolve()
                if not p.exists() or not p.is_file():
                    return {"ok": False, "error": f"文件不存在: {path}"}
                self._pending_attachments.append(str(p))
                return {"ok": True, "path": str(p), "message": "已登记，将在本轮回复后发送"}

            self.tool_registry.register(
                name="attach_file",
                description="登记本地文件，在本轮回复发出后作为附件发给用户。仅在通过消息网关（如 QQ / 微信）对话时可用。",
                parameters={
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "本地文件路径（绝对路径或相对于工作区的路径）。",
                        },
                    },
                    "required": ["path"],
                },
                function=attach_file,
            )

    def _run_subagent(
        self,
        prompt: str,
        description: str,
        subagent_type: str = "general-purpose",
        background: bool = True,
        model: dict[str, Any] | None = None,
        resume_from: str | None = None,
        timeout_ms: int = 600_000,
    ) -> dict[str, Any]:
        if not isinstance(prompt, str) or not prompt.strip():
            return {"ok": False, "error": "prompt 必须是非空字符串。"}
        if not isinstance(description, str) or not description.strip():
            return {"ok": False, "error": "description 必须是非空字符串。"}
        if not isinstance(background, bool):
            return {"ok": False, "error": "background 必须是布尔值。"}
        if (
            not isinstance(timeout_ms, int)
            or isinstance(timeout_ms, bool)
            or not 1 <= timeout_ms <= 600_000
        ):
            return {"ok": False, "error": "timeout_ms 必须是 1 到 600000 之间的整数。"}

        if subagent_type == "general":
            subagent_type = "general-purpose"
        plugin_agent = self.plugin_agents.get(subagent_type)
        if plugin_agent is not None:
            tool_names = [
                name
                for name in self.tool_registry._tools
                if name != "agent" and name not in GOAL_TOOL_NAMES
            ]
            aliases = {
                "Bash": "bash",
                "Read": "read_file",
                "Write": "write_file",
                "Edit": "patch_file",
                "Grep": "grep",
                "Glob": "glob",
                "WebSearch": "web_search",
                "WebFetch": "web_extract",
            }
            requested = {
                aliases.get(str(name), str(name))
                for name in plugin_agent["tools"]
            }
            if requested and "*" not in requested:
                tool_names = [name for name in tool_names if name in requested]
            denied = {
                aliases.get(str(name), str(name))
                for name in plugin_agent["disallowed_tools"]
            }
            tool_names = [name for name in tool_names if name not in denied]
            if plugin_agent["prompt_mode"] == "full":
                system_prompt = plugin_agent["prompt"]
            else:
                template = self.context_manager._read_text_file(
                    self.navi_home / "subagent-general-prompt.txt",
                    None,
                )
                base_prompt = self.context_manager._render_system_prompt_template(
                    template,
                    agents_md=self.context_manager.load_agents_md(),
                    skills_prompt=self.context_manager.build_skill_index_prompt(),
                ) if template else self._system_prompt
                system_prompt = f"{base_prompt}\n\n{plugin_agent['prompt']}"
            prompt_file = None
        elif subagent_type == "explore":
            tool_names = [n for n in EXPLORE_TOOLS if n in self.tool_registry._tools]
            prompt_file = "subagent-explore-prompt.txt"
        elif subagent_type == "plan":
            tool_names = [n for n in EXPLORE_TOOLS if n in self.tool_registry._tools]
            prompt_file = "subagent-plan-prompt.txt"
        elif subagent_type == "general-purpose":
            tool_names = [
                n
                for n in self.tool_registry._tools
                if n != "agent" and n not in GOAL_TOOL_NAMES
            ]
            prompt_file = "subagent-general-prompt.txt"
        else:
            return {
                "ok": False,
                "error": (
                    f"未知 subagent_type：{subagent_type}，"
                    "应为 general-purpose、explore、plan 或已启用的插件 agent。"
                ),
            }

        if prompt_file is not None:
            template = self.context_manager._read_text_file(self.navi_home / prompt_file, None)
            if not template and subagent_type == "plan":
                template = self.context_manager._read_text_file(
                    self.navi_home / "subagent-explore-prompt.txt",
                    None,
                )
                if template:
                    template += "\n\n只做分析，不修改文件。最终输出可直接执行的分步实施计划。"
            system_prompt = self.context_manager._render_system_prompt_template(
                template,
                agents_md=self.context_manager.load_agents_md(),
                skills_prompt=self.context_manager.build_skill_index_prompt(),
            ) if template else self._system_prompt

        source_meta = None
        if resume_from is not None:
            if (
                not isinstance(resume_from, str)
                or len(resume_from) != 10
                or not resume_from.startswith("a_")
                or any(char not in "0123456789abcdef" for char in resume_from[2:])
            ):
                return {"ok": False, "error": "resume_from 不是有效的子 agent ID。"}
            source_meta = self.agent_store.get_meta(resume_from)
            if source_meta is None:
                return {"ok": False, "error": f"找不到子 agent：{resume_from}。"}
            if source_meta.get("parent_session_id") != self.session_store.session_id:
                return {"ok": False, "error": "只能恢复当前父会话的子 agent。"}
            if source_meta.get("status") != "completed":
                return {"ok": False, "error": "只能恢复已完成的子 agent。"}
            if source_meta.get("agent_type") != subagent_type:
                return {"ok": False, "error": "恢复时 subagent_type 必须与原实例相同。"}
            model = source_meta.get("model")

        router = self.router
        if model:
            if not isinstance(model, dict):
                return {"ok": False, "error": "model 必须是对象。"}
            if (
                model.get("provider") != self.router.provider
                or model.get("model") != self.router.model
            ):
                router = ModelRouter(
                    get_config_path(),
                    provider=model.get("provider", ""),
                    model=model.get("model", ""),
                )
                if router._provider is None:
                    return {
                        "ok": False,
                        "error": f"模型不可用：provider={model.get('provider')!r} 未在 config.json 中配置。",
                    }

        parent_scope = self._require_scope()
        parent_session_id = self.session_store.session_id
        child_scope = TurnScope(threading.Event())
        stream_runner = ModelStreamRunner(router, child_scope.cancel_event)
        agent_id = self.agent_store.create(
            agent_type=subagent_type,
            system_prompt=system_prompt,
            tool_names=tool_names,
        )
        if source_meta is not None:
            self.agent_store.save_context(
                agent_id,
                self.agent_store.load_context(resume_from),
            )
        self.agent_store.update_meta(
            agent_id,
            status="running",
            parent_session_id=parent_session_id,
            description=description.strip(),
            model={"provider": router.provider, "model": router.model},
            resumed_from=resume_from,
        )

        def _exec(tool_call_id: str, name: str, args: dict[str, Any]) -> Any:
            token = CURRENT_TOOL_CONTEXT.set(
                ToolExecutionContext(scope=child_scope, tool_call_id=tool_call_id)
            )
            try:
                denied = self._handle_approval(
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    tool_args=args,
                    scope=child_scope,
                    hook_session_id=agent_id,
                )
                if denied is not None:
                    return denied
                started = time.monotonic()
                try:
                    result = self.tool_registry.invoke(name, args)
                except Exception as exc:
                    self.hooks.dispatch(
                        "PostToolUseFailure",
                        agent_id,
                        {
                            "toolName": name,
                            "toolUseId": tool_call_id,
                            "toolInput": args,
                            "error": str(exc),
                            "durationMs": round((time.monotonic() - started) * 1000),
                            "subagentType": subagent_type,
                        },
                        child_scope,
                    )
                    raise
                hook_payload = {
                    "toolName": name,
                    "toolUseId": tool_call_id,
                    "toolInput": args,
                    "durationMs": round((time.monotonic() - started) * 1000),
                    "subagentType": subagent_type,
                }
                failed = isinstance(result, dict) and result.get("ok") is False
                if failed:
                    hook_payload["error"] = str(
                        result.get("error") or "Tool returned ok=false"
                    )
                else:
                    hook_payload["toolResult"] = result
                self.hooks.dispatch(
                    "PostToolUseFailure" if failed else "PostToolUse",
                    agent_id,
                    hook_payload,
                    child_scope,
                )
                return result
            finally:
                CURRENT_TOOL_CONTEXT.reset(token)

        agent = prepare_agent(
            router=router,
            tool_names=tool_names,
            tool_registry=self.tool_registry,
            system_prompt=system_prompt,
            agent_id=agent_id,
            store=self.agent_store,
            tool_executor=_exec,
            scope=child_scope,
            stream_runner=stream_runner,
            max_steps=self.max_steps,
        )

        def _runner() -> dict[str, Any]:
            child_scope.attach_execution_thread()
            started = time.monotonic()
            status = "failed"
            self.hooks.dispatch(
                "SubagentStart",
                parent_session_id,
                {
                    "subagentId": agent_id,
                    "subagentType": subagent_type,
                    "description": description.strip(),
                },
                child_scope,
            )
            try:
                with tool_worker(child_scope):
                    result = agent.run(user_input=prompt)
                self.agent_store.update_meta(
                    agent_id,
                    status="completed",
                    steps=result.steps,
                    tool_calls=len(result.tool_calls_made),
                )
                status = "completed"
                return {
                    "success": result.success,
                    "content": result.content,
                    "subagent_type": subagent_type,
                    "steps": result.steps,
                    "tool_calls": len(result.tool_calls_made),
                    "resume_from_hint": agent_id,
                }
            except KeyboardInterrupt:
                status = "cancelled"
                self.agent_store.update_meta(agent_id, status="cancelled")
                raise
            except Exception as exc:
                self.agent_store.update_meta(
                    agent_id,
                    status="failed",
                    error=str(exc),
                )
                raise
            finally:
                self.hooks.dispatch(
                    "SubagentStop",
                    parent_session_id,
                    {
                        "subagentId": agent_id,
                        "subagentType": subagent_type,
                        "description": description.strip(),
                        "exitCode": (
                            0
                            if status == "completed"
                            else 130
                            if status == "cancelled"
                            else 1
                        ),
                        "durationMs": round((time.monotonic() - started) * 1000),
                    },
                )
                child_scope.close()

        context = CURRENT_TOOL_CONTEXT.get()
        guard = (
            nullcontext()
            if background
            else parent_scope.aborter(lambda: child_scope.cancel("父 agent 中断"))
        )
        with guard:
            try:
                task = self.task_manager.start_worker(
                    agent_id,
                    prompt,
                    self.workspace,
                    description=description.strip(),
                    target=_runner,
                    cancel=child_scope.cancel,
                    background=background,
                    tool_call_id=context.tool_call_id if context is not None else None,
                )
            except Exception as exc:
                child_scope.close()
                self.agent_store.update_meta(agent_id, status="failed", error=str(exc))
                return {"ok": False, "error": str(exc), "subagent_id": agent_id}

            if background:
                return {
                    "ok": True,
                    "task_id": agent_id,
                    "subagent_id": agent_id,
                    "status": task["status"],
                    "backgrounded": True,
                }
            task = self.task_manager.wait_tasks(
                [agent_id],
                timeout_ms=timeout_ms,
            )[0]

        if parent_scope.is_cancelled():
            raise KeyboardInterrupt("用户中断")
        if task["status"] == "running":
            task = (
                self.task_manager.background_current(task_id=agent_id)
                or self.task_manager.get_output([agent_id])[0]
            )
            if task["status"] == "running":
                return {
                    "ok": True,
                    "task_id": agent_id,
                    "subagent_id": agent_id,
                    "status": "running",
                    "backgrounded": True,
                }
        if task["status"] == "completed":
            return {
                "ok": True,
                "content": task["output"],
                "subagent_id": agent_id,
                "subagent_type": subagent_type,
                "steps": task.get("steps", 0),
                "tool_calls": task.get("tool_calls", 0),
                "duration_secs": task["duration_secs"],
                "resume_from_hint": agent_id,
            }
        return {
            "ok": False,
            "error": task.get("error") or task["output"] or f"子 agent 状态：{task['status']}",
            "subagent_id": agent_id,
            "status": task["status"],
        }

    def _init_mcp_tools(self) -> None:
        """初始化 MCP 工具（如果配置了的话）。"""
        try:
            from ..integrations.mcp_client import discover_mcp_tools, _MCP_AVAILABLE
            if _MCP_AVAILABLE:
                mcp_tools = discover_mcp_tools(self.tool_registry, self.mcp_servers)
                if mcp_tools:
                    logger.info("MCP: registered %d tool(s): %s", len(mcp_tools), ", ".join(mcp_tools))
        except Exception as e:
            logger.debug("MCP initialization failed (non-fatal): %s", e)

    def close(self) -> None:
        with self._turn_lock:
            self.scheduler.close()
            self.task_manager.shutdown()
            self.lsp.close()
            self.hooks.dispatch(
                "SessionEnd",
                self.session_store.session_id,
                {"reason": "shutdown"},
            )
