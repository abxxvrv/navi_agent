"""SubAgent — 通用子 agent，支持多轮 LLM ↔ tool 循环。"""

from __future__ import annotations

import json
from typing import Any, Callable, TYPE_CHECKING

from .interruptible import run_model_stream

if TYPE_CHECKING:
    from ..storage.agent_store import AgentInstanceStore
    from ..model.router import ModelRouter
    from ..model.request import ModelStreamRunner
    from ..tools.registry import ToolRegistry
    from .interrupt_scope import TurnScope


# explore 子 agent 的只读工具集（从父注册表中存在的工具里挑）
EXPLORE_TOOLS = (
    "list_dir",
    "read_file",
    "grep",
    "glob",
    "skill_view",
    "web_search",
    "web_extract",
    "vision_analyze",
    "search_session",
)


class SubAgentResult:
    """子 agent 执行结果。"""

    __slots__ = ("content", "tool_calls_made", "steps", "success")

    def __init__(
        self,
        content: str,
        tool_calls_made: list[dict[str, Any]],
        steps: int,
        success: bool,
    ):
        self.content = content
        self.tool_calls_made = tool_calls_made
        self.steps = steps
        self.success = success

    def __repr__(self) -> str:
        return (
            f"SubAgentResult(success={self.success}, steps={self.steps}, "
            f"tool_calls={len(self.tool_calls_made)}, content_len={len(self.content)})"
        )


class SubAgent:
    """轻量子 agent：messages + tools -> while 循环直到模型不再调工具。"""

    def __init__(
        self,
        router: ModelRouter,
        tools: list[dict[str, Any]],
        tool_handlers: dict[str, Callable[..., Any]],
        system_prompt: str | None = None,
        agent_id: str | None = None,
        store: AgentInstanceStore | None = None,
        tool_executor: Callable[[str, dict[str, Any]], Any] | None = None,
        scope: TurnScope | None = None,
        stream_runner: ModelStreamRunner | None = None,
    ):
        self.router = router
        self.tools = tools
        self.tool_handlers = tool_handlers
        self.system_prompt = system_prompt
        self.agent_id = agent_id
        self.store = store
        self.tool_executor = tool_executor
        self.scope = scope
        self.stream_runner = stream_runner
        self.context: list[dict[str, Any]] = []

    def run(
        self,
        user_input: str,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> SubAgentResult:
        """同步执行子 agent。超时由调用方（_run_subagent）从外部强制，不在此处自查。"""
        messages: list[dict[str, Any]] = []

        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})
        if self.context:
            messages.extend(self.context)
        elif context_messages:
            messages.extend(context_messages)
        messages.append({"role": "user", "content": user_input})

        all_tool_calls: list[dict[str, Any]] = []
        steps = 0
        # 记住本轮对话的起始位置，只持久化本轮新增部分
        new_turn_start = len(messages)

        while True:
            # Ctrl+C / 父 agent 取消（含外部超时触发的 set_interrupt）：被取消即抛出向上传播
            if self.scope is not None:
                self.scope.raise_if_cancelled()

            steps += 1

            # 调用 LLM（流式收集）
            content, tool_calls = self._call_llm(messages)

            # 没有 tool_calls → 结束
            if not tool_calls:
                # 只保存本轮新增的对话（不含 system prompt、context_messages、旧 context）
                self.context = [m for m in messages[new_turn_start:] if m.get("role") != "system"]
                self._persist()
                return SubAgentResult(
                    content=content,
                    tool_calls_made=all_tool_calls,
                    steps=steps,
                    success=True,
                )

            # 追加 assistant 消息（含 tool_calls）
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": content or ""}
            assistant_msg["tool_calls"] = tool_calls
            messages.append(assistant_msg)

            # 执行每个 tool call
            for tc in tool_calls:
                tc_id = tc["id"]
                tc_name = tc["function"]["name"]
                tc_args_raw = tc["function"].get("arguments", "{}")

                try:
                    tc_args = json.loads(tc_args_raw) if tc_args_raw else {}
                    if not isinstance(tc_args, dict):
                        tc_args = {}
                except (json.JSONDecodeError, TypeError):
                    tc_args = {}

                # 执行工具
                result = self._execute_tool(tc_name, tc_args)
                result_str = json.dumps(result, ensure_ascii=False)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": result_str,
                })

                all_tool_calls.append({
                    "name": tc_name,
                    "args": tc_args,
                    "result": result,
                })

    def _persist(self) -> None:
        """持久化上下文到 store。"""
        if self.store and self.agent_id:
            self.store.save_context(self.agent_id, self.context)
            self.store.update_meta(self.agent_id, status="completed")

    def _call_llm(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """流式调用 LLM，返回 (content, tool_calls)。"""
        # 有 scope + runner 时走可中断的轮询式流（Ctrl+C 能掐断卡住的流）；
        # 否则退回裸 chat_stream（如后台审查路径，行为不变）。
        if self.scope is not None and self.stream_runner is not None:
            stream = run_model_stream(
                self.scope,
                self.stream_runner,
                messages=messages,
                tools=self.tools if self.tools else [],
            )
        else:
            stream = self.router.chat_stream(
                messages=messages,
                tools=self.tools if self.tools else [],
            )

        content_parts: list[str] = []
        tool_calls_map: dict[int, dict[str, Any]] = {}

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta

            if delta.content:
                content_parts.append(delta.content)

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

        content = "".join(content_parts)
        tool_calls = [tool_calls_map[i] for i in sorted(tool_calls_map.keys())] if tool_calls_map else []
        return content, tool_calls

    def _execute_tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """执行单个工具。"""
        if name not in self.tool_handlers:
            return {"ok": False, "error": f"Unknown tool: {name}"}
        try:
            # tool_executor 存在时走父 runtime（带审批），否则直接调处理函数
            if self.tool_executor is not None:
                return self.tool_executor(name, args)
            return self.tool_handlers[name](**args)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def prepare_agent(
    router: ModelRouter,
    tool_names: list[str],
    tool_registry: ToolRegistry,
    system_prompt: str | None = None,
    agent_id: str | None = None,
    store: AgentInstanceStore | None = None,
    tool_executor: Callable[[str, dict[str, Any]], Any] | None = None,
    scope: TurnScope | None = None,
    stream_runner: ModelStreamRunner | None = None,
) -> SubAgent:
    """从父 ToolRegistry 按名字取工具，构造 SubAgent。

    Args:
        router: 复用主 agent 的模型路由。
        tool_names: 要给子 agent 的工具名列表。空列表 = 无工具（单轮 LLM 调用）。
        tool_registry: 父 agent 的工具注册表。
        system_prompt: 子 agent 的系统提示词，None = 无。
        agent_id: 恢复已有实例的 ID，None = 新建。
        store: 实例存储，传入时启用持久化。
        tool_executor: 工具执行回调 (name, args) -> result，传入时子工具走它（用于接入父审批）。
    """
    # 恢复模式：从 store 加载 meta 和 context
    if agent_id and store:
        meta = store.get_meta(agent_id)
        if meta is None:
            raise FileNotFoundError(f"Agent instance not found: {agent_id}")
        if meta.get("system_prompt") is not None:
            system_prompt = meta["system_prompt"]
        if meta.get("tool_names") is not None:
            tool_names = meta["tool_names"]
        context = store.load_context(agent_id)
    else:
        context = []

    tools: list[dict[str, Any]] = []
    handlers: dict[str, Callable[..., Any]] = {}

    for name in tool_names:
        if name not in tool_registry._tools:
            continue
        spec = tool_registry._tools[name]
        tools.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        })
        handlers[name] = spec.function

    agent = SubAgent(
        router=router,
        tools=tools,
        tool_handlers=handlers,
        system_prompt=system_prompt,
        agent_id=agent_id,
        store=store,
        tool_executor=tool_executor,
        scope=scope,
        stream_runner=stream_runner,
    )
    agent.context = context
    return agent
