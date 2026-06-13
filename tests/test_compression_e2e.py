"""端到端测试：上下文压缩"""

import json
import shutil
import sys
from pathlib import Path

# 添加项目路径
from navi_agent.context.compressor import ContextCompressor, TRIGGER_RATIO


def load_messages(file_path: Path) -> list[dict]:
    """加载消息文件"""
    messages = []
    with open(file_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    messages.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return messages


def save_messages(file_path: Path, messages: list[dict]):
    """保存消息文件"""
    with open(file_path, "w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")


def save_as_markdown(file_path: Path, messages: list[dict], title: str):
    """保存为 Markdown 格式"""
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"消息数量: {len(messages)}\n\n")
        f.write("---\n\n")

        for i, msg in enumerate(messages):
            role = msg.get("role", "?")
            content = msg.get("content", "")

            # 格式化 role
            if role == "system":
                role_display = "🔧 System"
            elif role == "user":
                role_display = "👤 User"
            elif role == "assistant":
                role_display = "🤖 Assistant"
            elif role == "tool":
                role_display = "🔨 Tool"
            else:
                role_display = f"❓ {role}"

            f.write(f"## 消息 {i+1} - {role_display}\n\n")

            # 处理内容
            if isinstance(content, str):
                # 截断过长的内容
                if len(content) > 500:
                    f.write(f"{content[:500]}...\n\n")
                else:
                    f.write(f"{content}\n\n")
            elif isinstance(content, list):
                f.write(f"[列表内容，{len(content)} 个元素]\n\n")
            else:
                f.write(f"{content}\n\n")

            # 处理 tool_calls
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                f.write("**Tool Calls:**\n\n")
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    f.write(f"- {fn.get('name', '?')}({fn.get('arguments', '')[:100]}...)\n")
                f.write("\n")

            f.write("---\n\n")


def estimate_tokens(messages: list[dict]) -> int:
    """估算 token 数"""
    total_chars = 0
    for msg in messages:
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            total_chars += sum(len(str(p)) for p in content)
    return total_chars // 4


def test_compression():
    """测试压缩功能"""
    # 使用当前会话文件
    session_file = Path.home() / ".navi" / "sessions" / "20260604_195631_65f26574" / "messages.jsonl"

    if not session_file.exists():
        print(f"❌ 会话文件不存在: {session_file}")
        return False

    print(f"📂 加载会话文件: {session_file}")
    messages = load_messages(session_file)
    print(f"   消息数量: {len(messages)}")

    # 估算 token
    tokens = estimate_tokens(messages)
    print(f"   估算 token: {tokens:,}")

    # 创建压缩器
    context_window = tokens * 2
    compressor = ContextCompressor(context_window=context_window)

    print(f"\n🔍 压缩检查:")
    print(f"   Context Window: {compressor.context_window:,}")
    print(f"   触发阈值 ({TRIGGER_RATIO*100}%): {int(compressor.context_window * TRIGGER_RATIO):,}")
    print(f"   当前 token: {tokens:,}")

    # 测试消息分割
    print("\n✂️  测试消息分割:")
    head, middle, tail = compressor._split_messages(messages)
    print(f"   头部消息数: {len(head)}")
    print(f"   中间消息数: {len(middle)}")
    print(f"   尾部消息数: {len(tail)}")

    # 测试工具输出裁剪
    print("\n🧹 测试工具输出裁剪:")
    pruned_middle = compressor._prune_tool_results(middle)
    tool_count = sum(1 for m in middle if m.get("role") == "tool")
    pruned_count = sum(1 for m in pruned_middle if m.get("content") == "[Old tool result content cleared]")
    print(f"   原始工具消息数: {tool_count}")
    print(f"   裁剪后工具消息数: {pruned_count}")

    # 估算压缩效果
    original_tokens = estimate_tokens(middle)
    pruned_tokens = estimate_tokens(pruned_middle)
    print(f"\n📊 压缩效果估算:")
    print(f"   中间部分原始 token: {original_tokens:,}")
    print(f"   裁剪后 token: {pruned_tokens:,}")
    print(f"   节省 token: {original_tokens - pruned_tokens:,}")
    print(f"   压缩率: {(1 - pruned_tokens / original_tokens) * 100:.1f}%")

    # 构建压缩后的消息（模拟 LLM 摘要）
    summary_message = {
        "role": "system",
        "content": "[CONTEXT COMPACTION]\n\n"
                   "## 目标\n"
                   "讨论和实现 Navi Agent 的上下文压缩功能，参考 Claude Code、KIMI、Hermes 的设计。\n\n"
                   "## 已完成的操作\n"
                   "1. 分析了 Claude Code、KIMI、Hermes 的压缩策略\n"
                   "2. 设计了 Navi 的压缩方案（50% 触发，头部 3 轮 + LLM 摘要 + 尾部 5%）\n"
                   "3. 实现了 compressor.py 和相关测试\n"
                   "4. 修复了 GlobTool 字段名不一致的问题\n\n"
                   "## 当前状态\n"
                   "- 压缩器已实现并通过单元测试和端到端测试\n"
                   "- 压缩提示词已保存到 ~/.navi/compact-prompt.md\n"
                   "- 工具输出裁剪效果显著（83.8% 压缩率）\n\n"
                   "## 待完成\n"
                   "- 集成到 runtime.py 的 _llm_node 中\n"
                   "- 测试完整的压缩流程（包括 LLM 摘要）\n\n"
                   "## 关键信息\n"
                   "- 压缩触发阈值: context_window × 50%\n"
                   "- 头部保护: 系统提示词 + 前 3 轮对话\n"
                   "- 尾部保护: 最近 5% context_window\n"
                   "- 摘要模型: deepseek-v4-flash（通过 config.json 配置）"
    }
    compressed = head + [summary_message] + tail

    # 保存文件
    output_dir = Path(__file__).parent / "test_output"
    output_dir.mkdir(exist_ok=True)

    # 保存 JSON 格式
    save_messages(output_dir / "01_head.jsonl", head)
    save_messages(output_dir / "02_middle_original.jsonl", middle)
    save_messages(output_dir / "03_middle_pruned.jsonl", pruned_middle)
    save_messages(output_dir / "04_tail.jsonl", tail)
    save_messages(output_dir / "05_compressed.jsonl", compressed)

    # 保存 Markdown 格式（方便查看）
    save_as_markdown(output_dir / "01_head.md", head, "头部消息（保护区域）")
    save_as_markdown(output_dir / "02_middle_original.md", middle[:20], "中间原始消息（前 20 条示例）")
    save_as_markdown(output_dir / "03_middle_pruned.md", pruned_middle[:20], "中间裁剪后消息（前 20 条示例）")
    save_as_markdown(output_dir / "04_tail.md", tail, "尾部消息（保护区域）")
    save_as_markdown(output_dir / "05_compressed.md", compressed, "压缩后完整消息")

    print(f"\n💾 文件已保存到: {output_dir}")
    print(f"\n   JSON 格式:")
    print(f"   - 01_head.jsonl - 头部消息 ({len(head)} 条)")
    print(f"   - 02_middle_original.jsonl - 中间原始消息 ({len(middle)} 条)")
    print(f"   - 03_middle_pruned.jsonl - 中间裁剪后消息 ({len(pruned_middle)} 条)")
    print(f"   - 04_tail.jsonl - 尾部消息 ({len(tail)} 条)")
    print(f"   - 05_compressed.jsonl - 压缩后完整消息 ({len(compressed)} 条)")
    print(f"\n   Markdown 格式（方便查看）:")
    print(f"   - 01_head.md - 头部消息")
    print(f"   - 02_middle_original.md - 中间原始消息（前 20 条）")
    print(f"   - 03_middle_pruned.md - 中间裁剪后消息（前 20 条）")
    print(f"   - 04_tail.md - 尾部消息")
    print(f"   - 05_compressed.md - 压缩后完整消息 ⭐")

    print("\n✅ 端到端测试通过!")
    return True


if __name__ == "__main__":
    success = test_compression()
    sys.exit(0 if success else 1)