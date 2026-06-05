"""上下文压缩器"""

import json
import os
import shutil
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .model_router import ModelRouter


def _get_navi_home() -> Path:
    """获取 Navi 配置目录"""
    return Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()

# 参数配置
TRIGGER_RATIO = 0.50
PROTECT_HEAD_ROUNDS = 3
PROTECT_TAIL_RATIO = 0.05
CLEARED_MESSAGE = "[Old tool result content cleared]"
MIN_SUMMARY_TOKENS = 2_000
MAX_SUMMARY_TOKENS = 12_000
SUMMARY_RATIO = 0.20


def _load_compact_prompt() -> str:
    """加载压缩提示词模板"""
    prompt_path = _get_navi_home() / "compact-prompt.md"
    if prompt_path.exists():
        return prompt_path.read_text(encoding="utf-8")
    # 默认提示词
    return """
Create a structured checkpoint summary for the conversation.

TURNS TO SUMMARIZE:
{content}

Be CONCRETE — include file paths, command outputs, error messages.

## Current Focus
[What we're working on now]

## Completed Tasks
- [Task]: [Outcome]

## Active Issues
- [Issue]: [Status]

## Important Context
- [Key information]
"""


COMPACT_PROMPT_TEMPLATE = _load_compact_prompt()


class ContextCompressor:
    def __init__(
        self,
        context_window: int,
        router: "ModelRouter | None" = None,
    ):
        self.context_window = context_window
        self.router = router

    def should_compress(self, prompt_tokens: int) -> bool:
        """检查是否需要压缩"""
        return prompt_tokens >= self.context_window * TRIGGER_RATIO

    def compress(
        self,
        messages: list[dict[str, Any]],
        messages_path: Path | None = None,
    ) -> list[dict[str, Any]]:
        """
        执行压缩

        Args:
            messages: 当前消息列表
            messages_path: 会话历史文件路径（用于备份）

        Returns:
            压缩后的消息列表
        """
        # 1. 分割消息
        head, middle, tail = self._split_messages(messages)

        if not middle:
            return messages  # 没有中间部分，不需要压缩

        # 2. 预处理中间部分
        pruned_middle = self._prune_tool_results(middle)

        # 3. LLM 摘要
        summary = self._generate_summary(pruned_middle)
        summary_message = {
            "role": "system",
            "content": f"[CONTEXT COMPACTION]\n\n{summary}",
        }

        # 4. 拼接
        compressed = head + [summary_message] + tail

        # 5. 备份并重写文件
        if messages_path:
            self._rotate_and_write(messages_path, compressed)

        return compressed

    def _split_messages(
        self, messages: list[dict[str, Any]]
    ) -> tuple[list, list, list]:
        """分割消息：头部、中间、尾部"""

        # 头部：系统提示词 + 前 3 轮
        head_end = self._find_head_end(messages)
        head = messages[:head_end]

        # 尾部：从后往前，保留 context_window * 5% 的 token
        tail_budget = int(self.context_window * PROTECT_TAIL_RATIO * 4)  # 字符数
        tail_start = self._find_tail_start(messages, tail_budget)
        tail = messages[tail_start:]

        # 中间：剩余部分
        middle = messages[head_end:tail_start]

        return head, middle, tail

    def _find_head_end(self, messages: list[dict[str, Any]]) -> int:
        """找到头部保护的结束位置（前 3 轮）"""
        user_count = 0
        for i, msg in enumerate(messages):
            if msg.get("role") == "user":
                user_count += 1
                if user_count > PROTECT_HEAD_ROUNDS:
                    return i
        return len(messages)

    def _find_tail_start(
        self, messages: list[dict[str, Any]], char_budget: int
    ) -> int:
        """找到尾部保护的起始位置"""
        total_chars = 0

        for i in range(len(messages) - 1, -1, -1):
            msg_chars = self._estimate_message_chars(messages[i])

            if total_chars + msg_chars > char_budget:
                return i + 1

            total_chars += msg_chars

        return 0

    def _estimate_message_chars(self, message: dict[str, Any]) -> int:
        """估算单条消息的字符数"""
        content = message.get("content", "")
        if isinstance(content, str):
            return len(content)
        elif isinstance(content, list):
            return sum(len(str(p)) for p in content)
        return 0

    def _estimate_tokens(self, messages: list[dict[str, Any]]) -> int:
        """估算消息的 token 数"""
        total_chars = sum(self._estimate_message_chars(m) for m in messages)
        return total_chars // 4

    def _prune_tool_results(
        self, middle: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """预处理：替换旧工具输出为占位符"""
        result = []
        for msg in middle:
            if msg.get("role") == "tool":
                result.append({**msg, "content": CLEARED_MESSAGE})
            else:
                result.append(msg)
        return result

    def _generate_summary(self, middle: list[dict[str, Any]]) -> str:
        """调用 LLM 生成摘要"""
        # 序列化消息
        content = self._serialize_for_summary(middle)

        # 计算摘要预算
        content_tokens = self._estimate_tokens(middle)
        budget = max(
            MIN_SUMMARY_TOKENS,
            min(int(content_tokens * SUMMARY_RATIO), MAX_SUMMARY_TOKENS),
        )

        # 使用模板生成提示词
        prompt = COMPACT_PROMPT_TEMPLATE.format(content=content)

        return self._call_llm(prompt, max_tokens=budget)

    def _serialize_for_summary(self, messages: list[dict[str, Any]]) -> str:
        """序列化消息为文本"""
        parts = []
        for msg in messages:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, str):
                parts.append(f"[{role}]: {content[:2000]}")
            elif isinstance(content, list):
                text = " ".join(str(p)[:500] for p in content)
                parts.append(f"[{role}]: {text[:2000]}")
        return "\n\n".join(parts)

    def _call_llm(self, prompt: str, max_tokens: int = MAX_SUMMARY_TOKENS) -> str:
        """调用 LLM 生成摘要"""
        if self.router is None:
            raise RuntimeError("ModelRouter 未配置，无法调用压缩模型")

        response = self.router.chat_stream_compression(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
        )

        # 收集响应内容
        content = ""
        for chunk in response:
            if chunk.choices and chunk.choices[0].delta.content:
                content += chunk.choices[0].delta.content

        return content

    def _rotate_and_write(
        self, messages_path: Path, messages: list[dict[str, Any]]
    ):
        """备份旧文件，写入新文件"""
        # 1. 找到下一个可用的备份文件名
        backup_path = self._next_available_rotation(messages_path)

        # 2. 备份旧文件
        shutil.move(str(messages_path), str(backup_path))

        # 3. 写入压缩后的消息
        with open(messages_path, "w", encoding="utf-8") as f:
            for msg in messages:
                f.write(json.dumps(msg, ensure_ascii=False) + "\n")

    def _next_available_rotation(self, path: Path) -> Path:
        """找到下一个可用的备份文件名"""
        base_name = path.stem
        suffix = path.suffix

        # 找到最大编号
        max_num = 0
        for entry in path.parent.iterdir():
            if entry.name.startswith(f"{base_name}_") and entry.name.endswith(
                suffix
            ):
                try:
                    num = int(entry.stem.split("_")[-1])
                    max_num = max(max_num, num)
                except ValueError:
                    pass

        return path.parent / f"{base_name}_{max_num + 1}{suffix}"
