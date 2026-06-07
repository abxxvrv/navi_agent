"""SubAgent — 通用子 agent，支持多轮 LLM ↔ tool 循环。"""

from __future__ import annotations

import json
from typing import Any, Callable, TYPE_CHECKING

if TYPE_CHECKING:
    from .model_router import ModelRouter
    from .tool_registry import ToolRegistry


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
    ):
        self.router = router
        self.tools = tools
        self.tool_handlers = tool_handlers

    def run(
        self,
        user_input: str,
        context_messages: list[dict[str, Any]] | None = None,
    ) -> SubAgentResult:
        """同步执行子 agent。"""
        messages: list[dict[str, Any]] = []

        if context_messages:
            messages.extend(context_messages)
        messages.append({"role": "user", "content": user_input})

        all_tool_calls: list[dict[str, Any]] = []
        steps = 0

        while True:
            steps += 1

            # 调用 LLM（流式收集）
            content, tool_calls = self._call_llm(messages)

            # 没有 tool_calls → 结束
            if not tool_calls:
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

    def _call_llm(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        """流式调用 LLM，返回 (content, tool_calls)。"""
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
            return self.tool_handlers[name](**args)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}


def prepare_agent(
    router: ModelRouter,
    tool_names: list[str],
    tool_registry: ToolRegistry,
) -> SubAgent:
    """从父 ToolRegistry 按名字取工具，构造 SubAgent。

    Args:
        router: 复用主 agent 的模型路由。
        tool_names: 要给子 agent 的工具名列表。空列表 = 无工具（单轮 LLM 调用）。
        tool_registry: 父 agent 的工具注册表。
    """
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

    return SubAgent(
        router=router,
        tools=tools,
        tool_handlers=handlers,
    )
