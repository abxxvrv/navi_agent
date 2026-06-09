from __future__ import annotations

import json
import random
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import os
import time
from pathlib import Path
from typing import Any, Literal

from .paths import load_navi_dotenv
from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .context_manager import ContextManager
from .history_utils import get_final_assistant_message
from .model_router import ModelRouter
from .paths import get_config_path, get_navi_home
from .session_store import SessionStore
from .tool import (
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
)
from .tool_registry import ToolRegistry
from .compressor import ContextCompressor
from .memory_store import MemoryStore
from .background_review import BackgroundReviewer
from .skill_manage import SkillManageTool

from .approval import (
    ApprovalDecision,
    ApprovalManager,
    UserApprovalChoice,
)

class AgentState(TypedDict):
    messages: list[dict[str, Any]]


AgentEventHandler = Callable[[dict[str, Any]], None]

ApprovalHandler = Callable[[ApprovalDecision], str | UserApprovalChoice | bool]


class EmptyModelResponseError(RuntimeError):
    pass


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
    ):
        load_navi_dotenv()

        self.workspace = Path(workspace).resolve()
        self.max_steps = max_steps
        self.max_retries_per_step = max(0, max_retries_per_step)
        self.event_handler = event_handler
        self.approval_handler = approval_handler
        self.on_output = on_output
        self.navi_home = get_navi_home()
        self.approval_manager = ApprovalManager(
            mode=approval_mode,
            workspace=self.workspace,
            navi_home=self.navi_home,
        )

        sessions_root = str(self.navi_home / "sessions")

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
            session_dir = Path(sessions_root) / resume_session_id
            self.session_store = SessionStore.from_existing(session_dir, root=sessions_root)
            self.conversation_history = self._valid_messages(self.session_store.messages)
            # resume 时使用会话自己记录的模型
            _provider = self.session_store.meta.get("provider") or _default_provider
            _model = self.session_store.meta.get("model") or _default_model
        else:
            # 新会话使用默认模型
            _provider = _default_provider
            _model = _default_model
            self.session_store = SessionStore(
                root=sessions_root,
                project_path=str(self.workspace),
                provider=_provider,
                model=_model,
            )
            self.conversation_history = []

        self.tool_registry = ToolRegistry()
        self.memory_store = MemoryStore()
        self.context_manager = ContextManager(
            workspace=str(self.workspace),
            skills_path=str(self.navi_home / "skills"),
            navi_home=str(self.navi_home),
            memory_store=self.memory_store,
        )

        self.router = ModelRouter(_config_path, provider=_provider, model=_model)
        self.last_usage: dict[str, int] = self.session_store.get_usage()

        # 初始化后台审查器
        self.reviewer = BackgroundReviewer(
            router=self.router,
            tool_registry=self.tool_registry,
        )

        # 构建系统提示词，session 内固定不变
        # resume 时优先复用旧 session 的系统提示词，保持一致性
        persisted_system = self._get_persisted_system_message()
        if persisted_system is not None:
            self._system_prompt = persisted_system
        else:
            skill_index_prompt = self.context_manager.build_skill_index_prompt()
            _messages = self.context_manager.build_runtime_messages(
                messages=[],
                extra_instructions=skill_index_prompt,
            )
            self._system_prompt: str = _messages[0]["content"] if _messages else ""

        self._register_tools()

        # 工具列表：新会话时存入 meta，resume 时从 meta 读取
        if resume_session_id and "tools" in self.session_store.meta:
            self._tools_for_api = self.session_store.meta["tools"]
        else:
            self._tools_for_api = self.tool_registry.to_openai_tools()
            self.session_store.meta["tools"] = self._tools_for_api
            self.session_store._write_meta()

        # 初始化上下文压缩器
        self.compressor = ContextCompressor(
            context_window=self.router.context_window,
            router=self.router,
        )

        self.graph = self._compile_graph()

    def _emit(self, event: dict[str, Any]) -> None:
        if self.event_handler is None:
            return

        try:
            self.event_handler(event)
        except Exception:
            pass

    def run_task(self, task: str) -> dict[str, Any]:
        return self._invoke_agent(task, keep_history=False)

    def run_turn(self, user_input: str) -> dict[str, Any]:
        return self._invoke_agent(user_input, keep_history=True)

    def list_tools(self) -> list[str]:
        return list(self.tool_registry._tools.keys())

    def get_model_info(self) -> dict[str, Any]:
        return {
            "current_provider": self.router.provider,
            "current_model": self.router.model,
            "current_model_name": self.router.model_name,
            "providers": self.router.list_providers(),
            "models": self.router.list_models(),
        }

    def switch_model(self, provider_name: str, model_name: str) -> bool:
        ok = self.router.switch_model(provider_name, model_name)
        if ok:
            self.session_store.meta["provider"] = provider_name
            self.session_store.meta["model"] = model_name
            self.session_store._write_meta()
        return ok

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
                if isinstance(tool_call, dict) and tool_call.get("id")
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

    # 调用agent，临时任务、对话模式都会用这个
    def _invoke_agent(self, user_input: str, keep_history: bool) -> dict[str, Any]:
        # 1. 清理和校验用户输入
        user_input = user_input.encode("utf-8", "replace").decode("utf-8").strip()
        if not user_input:
            return {
                "ok": False,
                "error": "user_input 不能为空。",
                "final_answer": "",
            }
        # 2. 准备上下文历史
        history = self.conversation_history if keep_history else []
        
        # 3. 构造当前用户消息
        user_message = {
            "role": "user",
            "content": user_input,
        }
        self._ensure_persisted_system_message()
        snapshot_len = len(self.session_store.messages)
        self.session_store.append_message(user_message)
        self.reviewer.user_message_count += 1

        # 4. 构造 graph 初始状态
        turn_state: AgentState = {
            "messages": [*history, user_message],
        }

        # 5. 执行 graph
        try:
            result = self.graph.invoke(
                turn_state,
                config={"recursion_limit": self.max_steps},
            )
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
            raise
        # 6. graph 异常处理
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "final_answer": "",
            }

        # 7. 取最终状态消息
        current_turn_messages = result["messages"]
        # 8. 提取最终回答
        final_message = get_final_assistant_message(current_turn_messages)

        if final_message is None:
            final_message = {
                "role": "assistant",
                "content": "",
            }

        final_answer = final_message.get("content", "")

        # 9. 更新 conversation_history
        if keep_history and final_answer:
            self.conversation_history = self._valid_messages(self.session_store.messages)

        return { # 返回CLI
            "ok": bool(final_answer),
            "final_answer": final_answer,
            "content": final_answer,
            "error": None if final_answer else "本轮没有得到有效最终回复。", # 用于给CLI判断是否正确的
            "messages": current_turn_messages,
            "session_id": self.session_store.session_id,
            "session_path": str(self.session_store.path),
        }

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

    def _compile_graph(self):
        graph_builder = StateGraph(AgentState)

        graph_builder.add_node("llm_node", self._llm_node)
        graph_builder.add_node("tool_node", self._tool_node)

        graph_builder.add_edge(START, "llm_node")
        graph_builder.add_conditional_edges(
            "llm_node",
            self._should_continue,
            {
                "tool_node": "tool_node",
                END: END,
            },
        )
        graph_builder.add_edge("tool_node", "llm_node")

        return graph_builder.compile()
    
    # 构造真正发给模型的 messages
    def _llm_node(self, state: AgentState) -> dict[str, Any]:
        messages = state["messages"]

        # 压缩检查
        prompt_tokens = self.last_usage.get("prompt_tokens", 0)
        if self.compressor.should_compress(prompt_tokens):
            try:
                compressed_messages = self.compressor.compress(
                    [
                        {"role": "system", "content": self._system_prompt},
                        *messages,
                    ],
                    messages_path=self.session_store.messages_path,
                )
                self.session_store.messages = compressed_messages
                messages = self._valid_messages(compressed_messages)
            except Exception as e:
                self._emit(
                    {
                        "type": "compress_error",
                        "message": f"上下文压缩失败，跳过: {e}",
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
                # 流式调用模型
                stream = self.router.chat_stream(
                    messages=model_messages,
                    tools=self._tools_for_api,
                )

                for chunk in stream:
                    # usage（最后一个 chunk，choices 通常为空）
                    if hasattr(chunk, "usage") and chunk.usage:
                        # prompt_tokens 只取最后一次（当前上下文大小）
                        self.last_usage["prompt_tokens"] = chunk.usage.prompt_tokens or 0
                        # completion_tokens 累加（全程生成总量）
                        self.last_usage["completion_tokens"] = self.last_usage.get("completion_tokens", 0) + (chunk.usage.completion_tokens or 0)
                        self.session_store.save_usage(self.last_usage)

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
                if not content.strip() and not tool_calls_map:
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

                if tool_calls_map:
                    if content:
                        self._emit({"type": "assistant_content", "content": content})
                    assistant_message["tool_calls"] = [
                        tool_calls_map[i] for i in sorted(tool_calls_map.keys())
                    ]

                self.session_store.append_message(assistant_message)

                return {
                    "messages": [*messages, assistant_message]
                }
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
        self._emit({"type": "tool_start", "tool_name": tool_name, "tool_args": tool_args})

        t0 = time.monotonic()
        try:
            tool_result = self.tool_registry.invoke(tool_name, tool_args)
        except Exception as exc:
            tool_result = {"ok": False, "error": str(exc)}
            self._emit({"type": "tool_error", "tool_name": tool_name,
                        "tool_args": tool_args, "error": str(exc)})
        else:
            elapsed = time.monotonic() - t0
            self._emit({"type": "tool_result", "tool_name": tool_name,
                        "tool_args": tool_args, "tool_result": tool_result,
                        "elapsed": elapsed})

        return (tool_call_id, tool_result, tool_name, tool_args)

    def _tool_node(self, state: AgentState) -> dict[str, Any]:
        last_message = state["messages"][-1]
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

        # 冲突检测：多个工具写同一文件则全部拒绝
        write_map: dict[str, list[int]] = defaultdict(list)
        for i, (call_id, tool_name, tool_args) in enumerate(parsed):
            if tool_name in ("write_file", "patch_file") and tool_args.get("path"):
                write_map[tool_args["path"]].append(i)

        conflict_indices = set()
        for path, indices in write_map.items():
            if len(indices) > 1:
                conflict_indices.update(indices)

        non_conflicting: list[tuple[str, str, dict]] = []
        for i, (call_id, tool_name, tool_args) in enumerate(parsed):
            if i in conflict_indices:
                error = {"ok": False, "error": f"冲突：多个工具同时写入同一文件 {tool_args.get('path')}"}
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": json.dumps(error, ensure_ascii=False),
                }
                self.session_store.append_message(tool_message)
                rejected_messages.append(tool_message)
            else:
                non_conflicting.append((call_id, tool_name, tool_args))
        parsed = non_conflicting

        # 阶段二：审批
        to_execute: list[tuple[str, str, dict]] = []  # (call_id, name, args)
        for call_id, tool_name, tool_args in parsed:
            approval_result = self._handle_approval(
                tool_call_id=call_id, tool_name=tool_name, tool_args=tool_args)
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

        # 阶段三：并发执行
        executed_messages: list[dict] = []
        if to_execute:
            with ThreadPoolExecutor(max_workers=len(to_execute)) as executor:
                futures = {
                    executor.submit(self._execute_single_tool, name, args, cid): cid
                    for cid, name, args in to_execute
                }
                results: dict[str, tuple] = {}
                for future in futures:
                    call_id, result, name, args = future.result()
                    results[call_id] = (call_id, result, name, args)

            # 收集多模态图片，稍后注入 user message
            pending_images: list[dict] = []

            for call_id, tool_name, tool_args in to_execute:
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
                    *state["messages"],
                    *tool_messages,
                ],
                "skill",
            )

        return {
            "messages": [*state["messages"], *tool_messages],
        }

    # 在工具节点去审批的函数
    def _handle_approval(
        self,
        tool_call_id: str,
        tool_name: str,
        tool_args: dict[str, Any],
    ) -> dict[str, Any] | None:
        # 根据工具名字和参数，生成一个选择：看看是允许、拒绝、问用户的哪一种
        decision = self.approval_manager.check_tool_call(tool_name, tool_args)

        if decision.is_allow: # 如果被允许，返回空。
            return None

        if decision.is_deny: # 如果被拒绝，会返回调用失败的结果作为工具结果。
            tool_result = decision.to_tool_error()
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
            user_choice = self.approval_handler(decision)
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
    
    # 判断图进入哪个节点
    def _should_continue(self, state: AgentState) -> Literal["tool_node", "__end__"]:
        last_message = state["messages"][-1]
        if last_message.get("tool_calls"):
            return "tool_node"
        return END

    def _register_tools(self) -> None:
        workspace = str(self.workspace)
        
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
            function=ReadFileTool(workspace=workspace),
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
            function=WriteFileTool(workspace=workspace),
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
            function=PatchTool(workspace=workspace),
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
            ),
        )

        # skill_manage
        self.tool_registry.register(
            name="skill_manage",
            description="""
- 管理技能文件：列出所有技能、查看某个技能的完整内容，或创建/更新技能。
- 只能在 skills/ 目录下操作。
- action="list"：列出所有可用技能（名称 + 简介）。
- action="read"：读取指定技能的完整 SKILL.md 内容，需要 name 参数。
- action="write"：创建或更新技能，需要 name 和 content 参数。
- content 必须是完整的 SKILL.md（含 YAML frontmatter）。
""",
            parameters={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write"],
                        "description": "操作类型：list（列出技能）、read（读取）、write（创建/更新）",
                    },
                    "name": {
                        "type": "string",
                        "description": "技能名称，如 skill-creator。read 和 write 时必填。",
                    },
                    "content": {
                        "type": "string",
                        "description": "完整的 SKILL.md 内容（含 YAML frontmatter），write 时必填。",
                    },
                },
                "required": ["action"],
            },
            function=SkillManageTool(),
        )

        # run_command
        self.tool_registry.register(
            name="run_command",
            description="""
- 可以运行短时间、非交互式的 Bash 命令。
- 写完代码后，使用该工具运行测试、检查语法或查看错误信息。
- 命令应使用 Bash 语法，不是 PowerShell 或 cmd 语法。
- 该工具会返回 stdout、stderr、output、exit_code、timed_out 和 shell。
- 不要运行会长期占用终端的服务命令；超时时间会被限制在工具允许范围内。
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
                        "description": "命令超时时间，单位秒。",
                        "default": 60,
                        "maximum": 300,
                    },
                    "encoding": {
                        "type": "string",
                        "description": "输出解码编码。",
                        "default": "utf-8",
                    },
                },
                "required": ["command"],
            },
            function=RunCommandTool(
                workspace=workspace,
                on_output=lambda *args, **kwargs: (
                    self.on_output(*args, **kwargs) if self.on_output else None
                ),
            ),
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
- 管理持久化记忆，跨会话保留。
- 存储目标 memory：你的笔记（环境事实、项目约定、工具特性、经验教训）。
- 存储目标 user：用户画像（用户偏好、沟通风格、工作习惯、技术栈）。
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
                        "description": "对图片的提问或指令，默认 '请描述这张图片的内容'。",
                        "default": "请描述这张图片的内容",
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
