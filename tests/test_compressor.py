"""Tests for compressor.py"""

import json
import pytest
from pathlib import Path
from navi_agent.compressor import (
    ContextCompressor,
    CLEARED_MESSAGE,
    TRIGGER_RATIO,
    PROTECT_HEAD_ROUNDS,
    PROTECT_TAIL_RATIO,
)


class TestContextCompressor:
    def setup_method(self):
        self.compressor = ContextCompressor(context_window=1_000_000)

    def test_should_compress_below_threshold(self):
        """低于阈值不压缩"""
        assert not self.compressor.should_compress(400_000)

    def test_should_compress_at_threshold(self):
        """达到阈值触发压缩"""
        assert self.compressor.should_compress(500_000)

    def test_should_compress_above_threshold(self):
        """超过阈值触发压缩"""
        assert self.compressor.should_compress(600_000)

    def test_find_head_end_with_enough_messages(self):
        """有足够消息时，头部保护前 3 轮"""
        messages = [
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "U3"},
            {"role": "assistant", "content": "A3"},
            {"role": "user", "content": "U4"},
        ]
        # 前 3 轮：U1, A1, U2, A2, U3, A3 = 6 条消息
        # 第 4 轮从 U4 开始，索引 6
        assert self.compressor._find_head_end(messages) == 6

    def test_find_head_end_with_few_messages(self):
        """消息不足 3 轮时，返回全部"""
        messages = [
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
        ]
        assert self.compressor._find_head_end(messages) == 2

    def test_find_head_end_skips_system(self):
        """头部保护跳过系统消息"""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "user", "content": "U2"},
            {"role": "assistant", "content": "A2"},
            {"role": "user", "content": "U3"},
            {"role": "assistant", "content": "A3"},
            {"role": "user", "content": "U4"},
        ]
        # 系统消息不计入轮次，前 3 轮从 U1 开始
        assert self.compressor._find_head_end(messages) == 7

    def test_find_tail_start(self):
        """尾部保护从后往前累加"""
        # 创建 300 条消息，每条 1000 字符
        messages = [{"role": "user", "content": "x" * 1000} for _ in range(300)]

        # 预算 100K chars
        tail_start = self.compressor._find_tail_start(messages, 100_000)

        # 应该保留最后 100 条消息（100K chars）
        assert tail_start > 0
        assert tail_start == 200  # 300 - 100

    def test_find_tail_start_all_fit(self):
        """所有消息都在预算内"""
        messages = [{"role": "user", "content": "x" * 100} for _ in range(10)]
        tail_start = self.compressor._find_tail_start(messages, 100_000)
        assert tail_start == 0

    def test_estimate_message_chars_string(self):
        """估算字符串内容的字符数"""
        msg = {"role": "user", "content": "hello"}
        assert self.compressor._estimate_message_chars(msg) == 5

    def test_estimate_message_chars_list(self):
        """估算列表内容的字符数"""
        msg = {"role": "user", "content": ["hello", "world"]}
        # str(["hello", "world"]) = "['hello', 'world']"
        chars = self.compressor._estimate_message_chars(msg)
        assert chars > 0

    def test_estimate_tokens(self):
        """估算 token 数"""
        messages = [{"role": "user", "content": "x" * 400}]
        assert self.compressor._estimate_tokens(messages) == 100

    def test_prune_tool_results(self):
        """工具结果被替换为占位符"""
        middle = [
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
            {"role": "tool", "content": "old result"},
        ]
        pruned = self.compressor._prune_tool_results(middle)

        assert pruned[0]["content"] == "U1"
        assert pruned[1]["content"] == "A1"
        assert pruned[2]["content"] == CLEARED_MESSAGE

    def test_prune_tool_results_preserves_non_tool(self):
        """非工具消息保持不变"""
        middle = [
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
        ]
        pruned = self.compressor._prune_tool_results(middle)
        assert pruned == middle

    def test_split_messages(self):
        """消息分割正确"""
        # 创建足够多的消息，中间部分要足够大
        messages = []
        # 系统提示词
        messages.append({"role": "system", "content": "System"})
        # 前 3 轮
        for i in range(3):
            messages.append({"role": "user", "content": f"U{i+1}"})
            messages.append({"role": "assistant", "content": f"A{i+1}"})
            messages.append({"role": "tool", "content": f"T{i+1}"})
        # 中间部分（每条 5000 字符，共 20 条 = 100K 字符）
        for i in range(20):
            messages.append({"role": "user", "content": f"MU{i+1}" * 5000})
            messages.append({"role": "assistant", "content": f"MA{i+1}" * 5000})
        # 尾部
        messages.append({"role": "user", "content": "Last U"})
        messages.append({"role": "assistant", "content": "Last A"})

        head, middle, tail = self.compressor._split_messages(messages)

        # 头部应该包含系统提示词 + 前 3 轮
        assert len(head) > 0
        assert head[0]["role"] == "system"

        # 尾部应该包含最后的消息
        assert len(tail) > 0
        assert tail[-1]["content"] == "Last A"

        # 中间应该有内容
        assert len(middle) > 0

    def test_next_available_rotation(self, tmp_path):
        """找到下一个可用的备份文件名"""
        # 创建测试文件
        messages_file = tmp_path / "messages.jsonl"
        messages_file.touch()

        # 创建一些备份
        (tmp_path / "messages_1.jsonl").touch()
        (tmp_path / "messages_2.jsonl").touch()

        next_path = self.compressor._next_available_rotation(messages_file)
        assert next_path.name == "messages_3.jsonl"

    def test_next_available_rotation_no_existing(self, tmp_path):
        """没有现有备份时"""
        messages_file = tmp_path / "messages.jsonl"
        messages_file.touch()

        next_path = self.compressor._next_available_rotation(messages_file)
        assert next_path.name == "messages_1.jsonl"

    def test_rotate_and_write(self, tmp_path):
        """备份并重写文件"""
        # 创建原始文件
        messages_file = tmp_path / "messages.jsonl"
        original_messages = [
            {"role": "user", "content": "U1"},
            {"role": "assistant", "content": "A1"},
        ]
        with open(messages_file, "w", encoding="utf-8") as f:
            for msg in original_messages:
                f.write(json.dumps(msg) + "\n")

        # 压缩后的消息
        compressed_messages = [
            {"role": "system", "content": "Summary"},
            {"role": "user", "content": "U2"},
        ]

        # 执行备份并重写
        self.compressor._rotate_and_write(messages_file, compressed_messages)

        # 验证备份文件存在
        backup_file = tmp_path / "messages_1.jsonl"
        assert backup_file.exists()

        # 验证备份文件内容
        with open(backup_file, "r", encoding="utf-8") as f:
            backup_content = f.read()
        assert "U1" in backup_content

        # 验证新文件内容
        with open(messages_file, "r", encoding="utf-8") as f:
            new_content = f.read()
        assert "Summary" in new_content
        assert "U2" in new_content
        assert "U1" not in new_content
