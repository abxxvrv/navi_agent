from typing import Any

# 从当前轮的所有 messages 里，倒着找出最后一个“真正的 assistant 最终回复”
def get_final_assistant_message(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        if message.get("role") != "assistant":
            continue

        if message.get("tool_calls"):
            continue

        content = message.get("content") or ""
        if not content.strip():
            continue

        if is_internal_status_message(content):
            continue

        return {
            "role": "assistant",
            "content": content,
        }

    return None

# 这个函数判断某段 assistant 内容是不是内部状态消息。
# 为什么要判断是不是内部状态信息？
def is_internal_status_message(content: str) -> bool:
    patterns = [
        "技能已成功加载",
        "技能已加载",
        "将在下一次模型调用时生效",
        "会在下一次模型调用时加载到系统提示词中",
    ]

    return any(pattern in content for pattern in patterns)
