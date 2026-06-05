"""后台审查模块 - 记忆和技能的自动审查"""

import json
import threading
from typing import Any

from .paths import get_navi_home


def _load_review_prompt(filename: str) -> str:
    """加载审查提示词"""
    path = get_navi_home() / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# 启动时加载提示词
MEMORY_REVIEW_PROMPT = _load_review_prompt("memory-review-prompt.md")
SKILL_REVIEW_PROMPT = _load_review_prompt("skill-review-prompt.md")


class BackgroundReviewer:
    def __init__(
        self,
        router: Any,
        tool_registry: Any,
    ):
        self.router = router
        self.tool_registry = tool_registry
        self.user_message_count = 0
        self.tool_turn_count = 0
        self.pending_message: str | None = None

    def spawn_review(self, messages: list[dict], review_type: str) -> None:
        """启动审查（后台线程）"""
        prompt = self._build_review_prompt(review_type)
        if not prompt:
            return

        # 组装完整消息：对话历史 + 审查提示词
        full_messages = list(messages) + [{"role": "user", "content": prompt}]

        # 获取相关工具
        if review_type == "memory":
            tools = [t for t in self.tool_registry.to_openai_tools()
                     if t["function"]["name"] == "memory"]
        else:
            tools = [t for t in self.tool_registry.to_openai_tools()
                     if t["function"]["name"] == "skill_manage"]

        thread = threading.Thread(
            target=self._run_review,
            args=(full_messages, tools, review_type),
            daemon=True,
        )
        thread.start()

    def _build_review_prompt(self, review_type: str) -> str:
        """根据类型构建提示词"""
        if review_type == "memory":
            return MEMORY_REVIEW_PROMPT
        # 技能反思时追加 skill-creator 规范
        skill_creator = _load_review_prompt("skills/skill-creator/SKILL.md")
        if skill_creator:
            return SKILL_REVIEW_PROMPT + "\n\n---\n\n" + skill_creator
        return SKILL_REVIEW_PROMPT

    def _run_review(self, messages: list[dict], tools: list[dict], review_type: str) -> None:
        """执行审查（线程函数）"""
        try:
            # 调用主模型
            response = self.router.chat_stream(
                messages=messages,
                tools=tools,
            )

            # 收集响应
            content = ""
            tool_calls = []
            for chunk in response:
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    if delta.content:
                        content += delta.content
                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            # 收集工具调用
                            while len(tool_calls) <= tc.index:
                                tool_calls.append({"name": "", "arguments": ""})
                            if tc.function.name:
                                tool_calls[tc.index]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls[tc.index]["arguments"] += tc.function.arguments

            # 执行工具调用
            executed = False
            for tc in tool_calls:
                if tc["name"]:
                    self._execute_tool(tc["name"], tc["arguments"])
                    executed = True

            # 只有实际执行了修改才通知用户
            if executed:
                self.pending_message = "Navi 已进行自我提升"

        except Exception:
            # 静默失败，不影响主流程
            pass

    def _execute_tool(self, tool_name: str, arguments_json: str) -> None:
        """执行工具调用"""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
            self.tool_registry.invoke(tool_name, args)
        except Exception:
            pass
