"""后台审查模块 - 记忆和技能的自动审查（基于 SubAgent 多轮执行）"""

import threading
from typing import Any, TYPE_CHECKING

from .paths import get_navi_home
from .sub_agent import prepare_agent

if TYPE_CHECKING:
    from .model_router import ModelRouter
    from .tool_registry import ToolRegistry


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
        router: "ModelRouter",
        tool_registry: "ToolRegistry",
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

        thread = threading.Thread(
            target=self._run_review,
            args=(messages, prompt, review_type),
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

    def _run_review(self, messages: list[dict], prompt: str, review_type: str) -> None:
        """执行审查（线程函数，通过 SubAgent 多轮执行）"""
        try:
            # 选工具
            if review_type == "memory":
                tool_names = ["memory"]
            else:
                tool_names = ["skill_manage"]

            agent = prepare_agent(
                router=self.router,
                system_prompt="",
                tool_names=tool_names,
                tool_registry=self.tool_registry,
            )

            result = agent.run(
                user_input=prompt,
                context_messages=messages if messages else None,
            )

            # 只有实际执行了修改才通知用户
            if result.tool_calls_made:
                self.pending_message = "Navi 已进行自我提升"

        except Exception:
            # 静默失败，不影响主流程
            pass
