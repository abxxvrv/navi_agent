from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import operator
import os
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import load_dotenv
from langgraph.graph import END, START, StateGraph
from openai import OpenAI
from typing_extensions import TypedDict

from .context_manager import ContextManager
from .history_utils import build_turn_record, get_final_assistant_message
from .paths import get_navi_home
from .session_store import SessionStore
from .tool import (
    ListDirTool,
    LoadSkillTool,
    PatchTool,
    ReadFileTool,
    RunCommandTool,
    SearchSessionHistoryTool,
    SkillViewTool,
    WriteFileTool,
)
from .tool_registry import ToolRegistry

from .approval import (
    ApprovalDecision,
    ApprovalManager,
    UserApprovalChoice,
)

class AgentState(TypedDict):
    messages: Annotated[list[dict[str, Any]], operator.add]
    active_skills: list[str]


AgentEventHandler = Callable[[dict[str, Any]], None]

ApprovalHandler = Callable[[ApprovalDecision], str | UserApprovalChoice | bool]

class AgentRuntime:
    def __init__(
        self,
        workspace: str | Path = ".",
        model: str = "deepseek-v4-flash",
        max_steps: int = 120,
        event_handler: AgentEventHandler | None = None,
        approval_mode: str = "normal",
        approval_handler: ApprovalHandler | None = None,
        resume_session_id: str | None = None,
    ):
        load_dotenv()

        self.workspace = Path(workspace).resolve()
        self.model = model
        self.max_steps = max_steps
        self.event_handler = event_handler
        self.approval_handler = approval_handler
        self.approval_manager = ApprovalManager(mode=approval_mode)
        self.navi_home = get_navi_home()

        sessions_root = str(self.navi_home / "sessions")

        if resume_session_id:
            session_dir = Path(sessions_root) / resume_session_id
            self.session_store = SessionStore.from_existing(session_dir, root=sessions_root)
            self.semantic_history = []
            for turn in self.session_store.turns:
                user_content = turn.get("user", "")
                assistant_content = turn.get("assistant", "")
                if user_content:
                    self.semantic_history.append({"role": "user", "content": user_content})
                if assistant_content:
                    self.semantic_history.append({"role": "assistant", "content": assistant_content})
            self.turn_id = len(self.session_store.turns)
        else:
            self.session_store = SessionStore(
                root=sessions_root,
                project_path=str(self.workspace),
            )
            self.semantic_history = []
            self.turn_id = 0

        self.tool_registry = ToolRegistry()
        self.context_manager = ContextManager(
            workspace=str(self.workspace),
            skills_path=str(self.navi_home / "skills"),
            navi_home=str(self.navi_home),
        )
        self.active_skills: list[str] = []
        self.current_turn_id: int | None = None

        self.client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        )

        self._register_tools()
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
    
    # 调用agent，临时任务、对话模式都会用这个
    def _invoke_agent(self, user_input: str, keep_history: bool) -> dict[str, Any]:
        # 1. 清理和校验用户输入
        user_input = user_input.strip()
        if not user_input:
            return {
                "ok": False,
                "error": "user_input 不能为空。",
                "final_answer": "",
            }
        # 2. 准备上下文历史
        base_history = self.semantic_history if keep_history else []
        
        # 3. 构造当前用户消息
        user_message = {
            "role": "user",
            "content": user_input,
        }
        
        # 4. 重置 active_skills
        # 每次任务/对话轮次都从干净的 active_skills 开始，避免上一轮技能污染。
        self.active_skills = []
        
        # 5. 构造 graph 初始状态
        turn_state: AgentState = {
            "messages": [*base_history, user_message],
            "active_skills": self.active_skills,
        }
        
        # 6. 设置当前 turn_id 并实时写 turn_start
        self.current_turn_id = self.turn_id
        self.session_store.append_event(
            {
                "turn_id": self.current_turn_id,
                "type": "turn_start",
                "user": user_input,
                "keep_history": keep_history,
            }
        )
        
        # 7. 执行 graph
        try:
            result = self.graph.invoke(
                turn_state,
                config={"recursion_limit": self.max_steps},
            )
        # 8. graph 异常处理
        except Exception as exc:
            self.session_store.append_event(
                {
                    "turn_id": self.current_turn_id,
                    "type": "turn_error",
                    "ok": False,
                    "error": str(exc),
                }
            )
            self.turn_id += 1
            self.current_turn_id = None
            return {
                "ok": False,
                "error": str(exc),
                "final_answer": "",
            }
        
        # 9. 截取当前轮消息
        current_turn_messages = result["messages"][  len(base_history)  :  ] 
        # 10. 提取最终回答
        final_message = get_final_assistant_message(current_turn_messages)

        if final_message is None:
            final_message = {
                "role": "assistant",
                "content": "",
            }
        # 11. 同步 active_skills
        self.active_skills = list(result.get("active_skills", []))
        
        # 12. 构造 turn_record
        turn_record = build_turn_record(
            turn_id=self.turn_id,
            user_input=user_input,
            final_message=final_message,
        )

        # 13. 写 turns.jsonl 和 turn_end
        final_answer = final_message.get("content", "")
        self.session_store.append_turn(turn_record)
        self.session_store.append_event(
            {
                "turn_id": self.current_turn_id,
                "type": "turn_end",
                "ok": bool(final_answer),
                "final_answer": final_answer,
                "active_skills": self.active_skills,
            }
        )
        self.turn_id += 1
        self.current_turn_id = None

        # 14. 更新 semantic_history 
        if keep_history and final_answer:
            self.semantic_history.append(user_message)
            self.semantic_history.append(
                {
                    "role": "assistant",
                    "content": final_answer,
                }
            )

        return { # 返回CLI
            "ok": bool(final_answer),
            "final_answer": final_answer,
            "content": final_answer,
            "error": None if final_answer else "本轮没有得到有效最终回复。", # 用于给CLI判断是否正确的
            "active_skills": self.active_skills,
            "messages": current_turn_messages,
            "session_id": self.session_store.session_id,
            "session_path": str(self.session_store.path),
        }

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

    def _llm_node(self, state: AgentState) -> dict[str, Any]:
        skill_index_prompt = self.context_manager.build_skill_index_prompt()
        runtime_messages = self.context_manager.build_runtime_messages(
            messages=state["messages"],
            active_skills=state.get("active_skills", []),
            extra_instructions=skill_index_prompt,
        )

        response = self.client.chat.completions.create(
            model=self.model,
            messages=runtime_messages,
            tools=self.tool_registry.to_openai_tools(),
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )

        return {
            "messages": [
                self._assistant_message_to_dict(response.choices[0].message)
            ]
        }

    def _execute_single_tool(
        self, tool_name: str, tool_args: dict, tool_call_id: str
    ) -> tuple[str, Any, str, dict]:
        """执行单个工具，供线程池调用。返回 (tool_call_id, result, name, args)。"""
        self._emit({"type": "tool_start", "tool_name": tool_name, "tool_args": tool_args})

        try:
            tool_result = self.tool_registry.invoke(tool_name, tool_args)
        except Exception as exc:
            tool_result = {"ok": False, "error": str(exc)}
            self.session_store.append_event(
                {"turn_id": self.current_turn_id, "type": "tool_error",
                 "tool_call_id": tool_call_id, "tool_name": tool_name,
                 "arguments": tool_args, "error": str(exc)}
            )
            self._emit({"type": "tool_error", "tool_name": tool_name,
                        "tool_args": tool_args, "error": str(exc)})
        else:
            self.session_store.append_event(
                {"turn_id": self.current_turn_id, "type": "tool_result",
                 "tool_call_id": tool_call_id, "tool_name": tool_name,
                 "arguments": tool_args,
                 "ok": bool(tool_result.get("ok")) if isinstance(tool_result, dict) else True,
                 "summary": self._summarize_tool_result(tool_result),
                 "result": tool_result}
            )
            self._emit({"type": "tool_result", "tool_name": tool_name,
                        "tool_args": tool_args, "tool_result": tool_result})

        return (tool_call_id, tool_result, tool_name, tool_args)

    def _tool_node(self, state: AgentState) -> dict[str, Any]:
        last_message = state["messages"][-1]
        active_skills = list(state.get("active_skills", []))
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
                self.session_store.append_event(
                    {"turn_id": self.current_turn_id, "type": "tool_error",
                     "tool_call_id": tool_call["id"], "tool_name": tool_name,
                     "error": str(exc)}
                )
                self._emit({"type": "tool_error", "tool_name": tool_name,
                            "tool_args": {}, "error": str(exc)})
                failed_messages.append({"role": "tool", "tool_call_id": tool_call["id"],
                                        "content": json.dumps(tool_result, ensure_ascii=False)})
                continue
            self.session_store.append_event(
                {"turn_id": self.current_turn_id, "type": "tool_call",
                 "tool_call_id": tool_call["id"], "tool_name": tool_name,
                 "arguments": tool_args}
            )
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
                self.session_store.append_event(
                    {"turn_id": self.current_turn_id, "type": "tool_result",
                     "tool_call_id": call_id, "tool_name": tool_name,
                     "arguments": tool_args, "ok": False,
                     "summary": self._summarize_tool_result(error), "result": error}
                )
                rejected_messages.append({"role": "tool", "tool_call_id": call_id,
                                          "content": json.dumps(error, ensure_ascii=False)})
            else:
                non_conflicting.append((call_id, tool_name, tool_args))
        parsed = non_conflicting

        # 阶段二：审批
        to_execute: list[tuple[str, str, dict]] = []  # (call_id, name, args)
        for call_id, tool_name, tool_args in parsed:
            approval_result = self._handle_approval(
                tool_call_id=call_id, tool_name=tool_name, tool_args=tool_args)
            if approval_result is not None:
                self.session_store.append_event(
                    {"turn_id": self.current_turn_id, "type": "tool_result",
                     "tool_call_id": call_id, "tool_name": tool_name,
                     "arguments": tool_args, "ok": False,
                     "summary": self._summarize_tool_result(approval_result),
                     "result": approval_result}
                )
                rejected_messages.append({"role": "tool", "tool_call_id": call_id,
                                          "content": json.dumps(approval_result, ensure_ascii=False)})
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

            for call_id, tool_name, tool_args in to_execute:
                _, tool_result, _, _ = results[call_id]
                if (tool_name == "load_skill"
                        and isinstance(tool_result, dict)
                        and tool_result.get("ok")):
                    skill_name = tool_result["skill_name"]
                    if skill_name not in active_skills:
                        active_skills.append(skill_name)
                executed_messages.append(
                    {"role": "tool", "tool_call_id": call_id,
                     "content": json.dumps(tool_result, ensure_ascii=False)}
                )

        return {
            "messages": failed_messages + rejected_messages + executed_messages,
            "active_skills": active_skills,
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
        # 记录一下进入审批了
        self.session_store.append_event(
            {
                "turn_id": self.current_turn_id,
                "type": "approval_check",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": tool_args,
                "action": decision.action.value,
                "risk": decision.risk.value,
                "reason": decision.reason,
                "approval_key": decision.approval_key,
                "command": decision.command,
            }
        )

        if decision.is_allow: # 如果被允许，返回空。
            return None

        if decision.is_deny: # 如果被拒绝，会返回调用失败的结果作为工具结果。
            tool_result = decision.to_tool_error()
            self.session_store.append_event(
                {
                    "turn_id": self.current_turn_id,
                    "type": "approval_denied",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": tool_args,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "command": decision.command,
                }
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
            self.session_store.append_event(
                {
                    "turn_id": self.current_turn_id,
                    "type": "approval_error",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": tool_args,
                    "risk": decision.risk.value,
                    "error": str(exc),
                    "command": decision.command,
                }
            )
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
            self.session_store.append_event(
                {
                    "turn_id": self.current_turn_id,
                    "type": "approval_approved",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                    "arguments": tool_args,
                    "risk": decision.risk.value,
                    "reason": decision.reason,
                    "approval_key": decision.approval_key,
                    "command": decision.command,
                }
            )
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
        self.session_store.append_event(
            {
                "turn_id": self.current_turn_id,
                "type": "approval_rejected",
                "tool_call_id": tool_call_id,
                "tool_name": tool_name,
                "arguments": tool_args,
                "risk": decision.risk.value,
                "reason": reason,
                "command": decision.command,
            }
        )
        self._emit(
            {
                "type": "tool_error",
                "tool_name": tool_name,
                "tool_args": tool_args,
                "error": reason,
            }
        )
        return tool_result
    
    # 摘要，现在就是按关键词随便匹配。然后内容存入了 event.jsonl，里面也包括了完整的工具调用结果。
    def _summarize_tool_result(self, result: Any) -> str:
        if not isinstance(result, dict):
            return str(result)[:500]

        parts = []
        for key in ("ok", "path", "command", "exit_code", "error"):
            if key in result:
                parts.append(f"{key}={result.get(key)}")

        output = str(result.get("output") or "")
        if output:
            parts.append(f"output={output[:300]}")

        if not parts:
            return json.dumps(result, ensure_ascii=False)[:500]

        return " | ".join(parts)[:500]

    def _should_continue(self, state: AgentState) -> Literal["tool_node", "__end__"]:
        last_message = state["messages"][-1]
        if last_message.get("tool_calls"):
            return "tool_node"
        return END

    def _assistant_message_to_dict(self, message: Any) -> dict[str, Any]:
        data = {
            "role": "assistant",
            "content": message.content or "",
        }

        reasoning_content = getattr(message, "reasoning_content", None)
        if reasoning_content is not None:
            data["reasoning_content"] = reasoning_content

        if message.tool_calls:
            data["tool_calls"] = [
                tool_call.model_dump(exclude_none=True)
                for tool_call in message.tool_calls
            ]

        return data

    def _register_tools(self) -> None:
        workspace = str(self.workspace)
        
        # get_date
        self.tool_registry.register(
            name="get_date",
            description="获取当前日期。",
            parameters={
                "type": "object",
                "properties": {},
            },
            function=lambda: datetime.now().strftime("%Y-%m-%d"),
        )
        
        # get_weather
        self.tool_registry.register(
            name="get_weather",
            description="获取指定地点在指定日期的天气。用户需要提供地点和日期。",
            parameters={
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "城市名称。",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期，格式为 YYYY-mm-dd。",
                    },
                },
                "required": ["location", "date"],
            },
            function=lambda location, date: f"{location} 在 {date} 的天气：多云，7~13°C",
        )
        
        # list_dir
        self.tool_registry.register(
            name="list_dir",
            description="列出工作区内指定路径下的文件和目录。",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作区的目录路径。使用 '.' 表示工作区根目录。",
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
            description=(
                "读取工作区内的 UTF-8 文本文件。"
                "当用户要求查看、解释、总结或调试文件时使用。"
                "路径必须是相对于工作区的路径。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作区的文件路径。",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "读取的起始行号，从 1 开始。",
                        "default": 1,
                        "minimum": 1,
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "最多读取的行数。",
                        "default": 200,
                        "minimum": 1,
                        "maximum": 500,
                    },
                },
                "required": ["path"],
            },
            function=ReadFileTool(workspace=workspace),
        )

        # write_file
        self.tool_registry.register(
            name="write_file",
            description=(
                "向工作区内的文本文件写入内容。"
                "用于创建新文件、追加内容，或者在明确需要时替换整个文件。"
                "小范围修改已有文件时优先使用 patch_file。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要写入的工作区相对路径。",
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
            description=(
                "对工作区内已有文本文件进行局部修改。"
                "通过精确匹配 old_text 并替换为 new_text 来修改文件。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对于工作区的文件路径。",
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
            description=(
                "查看指定技能的 SKILL.md 内容，但不激活该技能。"
                "当用户想查看、解释、总结、检查或调试某个技能时使用。"
                "如果需要让技能在后续模型调用中生效，应使用 load_skill。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能文件夹名称，例如 skill-creator、docx、safe-refactor。",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回多少字符，防止技能文件过长。",
                        "default": 20000,
                        "minimum": 1000,
                        "maximum": 100000,
                    },
                    "encoding": {
                        "type": "string",
                        "description": "文本编码，通常使用 utf-8。",
                        "default": "utf-8",
                    },
                },
                "required": ["name"],
            },
            function=SkillViewTool(
                workspace=workspace,
                skills_path=str(self.navi_home / "skills"),
            ),
        )

        # load_skill
        self.tool_registry.register(
            name="load_skill",
            description=(
                "按名称加载一个技能，使其在下一次模型调用时进入系统提示词。"
                "如果当前任务需要加载技能，请优先单独调用本工具；不要在同一轮同时调用其他工具。"
                "技能会在下一次模型调用时生效。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "技能索引中的技能文件夹名称。",
                    },
                },
                "required": ["name"],
            },
            function=LoadSkillTool(
                workspace=workspace,
                skills_path=str(self.navi_home / "skills"),
            ),
        )

        # run_command
        self.tool_registry.register(
            name="run_command",
            description=(
                "在工作区内运行一个短时间、非交互式的终端命令。"
                "主要用于验证代码、运行测试、检查语法或查看错误信息。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要运行的短时间、非交互式命令。",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "命令运行目录，必须是工作区内相对路径。",
                        "default": ".",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "命令超时时间，单位秒。",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 30,
                    },
                    "encoding": {
                        "type": "string",
                        "description": "输出解码编码。",
                        "default": "utf-8",
                    },
                },
                "required": ["command"],
            },
            function=RunCommandTool(workspace=workspace),
        )

        # search_session_history
        self.tool_registry.register(
            name="search_session_history",
            description=(
                "搜索 Navi 的全部会话历史，包括历史会话和当前会话。"
                "Navi 的会话历史保存在 NAVI_HOME 指定目录或用户主目录 .navi/sessions 下，主要有 3 种 jsonl 文件："
                "1. index.jsonl：位于 sessions 根目录，每行是一个 session 索引，记录 session_id、title、created_at、updated_at、project_path、turn_count。"
                "2. <session_id>/turns.jsonl：位于每个 session 目录内，每行是一轮语义历史，记录 turn_id、created_at、user、assistant。"
                "当 include_trace=false 时，搜索工具只搜索 turns.jsonl，适合回答用户之前问过什么、助手之前回答过什么。"
                "3. <session_id>/events.jsonl：位于每个 session 目录内，每行是一个执行事件，记录 turn_start、tool_call、tool_result、tool_error、turn_error、turn_end。"
                "当 include_trace=true 时，搜索工具只搜索 events.jsonl，适合回答工具调用、命令结果、执行过程、为什么这样做、读了哪个文件、改了什么文件等问题。"
                "不要用它搜索项目文件；搜索项目文件应使用 list_dir/read_file。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "搜索查询。可以搜索用户之前的问题、助手之前的回答，"
                            "或者在 include_trace=true 时搜索工具名、命令、文件路径、错误信息。"
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": "最多返回多少条记录。",
                        "default": 5,
                        "minimum": 1,
                        "maximum": 20,
                    },
                    "include_trace": {
                        "type": "boolean",
                        "description": (
                            "是否搜索执行轨迹。"
                            "默认 false，只搜索 <session_id>/turns.jsonl 中的用户输入和最终回答。"
                            "当用户询问工具调用、命令输出、执行过程、为什么这样做、读了哪个文件、改了什么文件时，设置为 true，"
                            "此时只搜索 <session_id>/events.jsonl。"
                        ),
                        "default": False,
                    },
                    "snippet_chars": {
                        "type": "integer",
                        "description": (
                            "每条结果返回的上下文字符数（匹配位置前后各截取的字符数）。"
                            "默认 300，范围 50-2000。较小的值节省上下文 token，较大的值提供更多上下文。"
                        ),
                        "default": 300,
                        "minimum": 50,
                        "maximum": 2000,
                    }
                },
                "required": ["query"],
            },
            function=SearchSessionHistoryTool(session_store=self.session_store),
        )
    
