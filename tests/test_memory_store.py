"""Tests for memory_store.py"""

import pytest
import tempfile
from pathlib import Path
from unittest.mock import patch

from navi_agent.memory_store import MemoryStore, ENTRY_DELIMITER


@pytest.fixture
def memory_store(tmp_path):
    """创建临时目录的 MemoryStore"""
    with patch("memory_store.get_memory_dir", return_value=tmp_path / "memories"):
        store = MemoryStore(memory_limit=100, user_limit=80)
        yield store


class TestMemoryStore:
    def test_init_empty(self, memory_store):
        """初始化时条目为空"""
        assert memory_store.memory_entries == []
        assert memory_store.user_entries == []

    def test_add_memory(self, memory_store):
        """添加记忆条目"""
        result = memory_store.add("memory", "这是测试记忆")
        assert result["success"] is True
        assert "这是测试记忆" in result["entries"]

    def test_add_user(self, memory_store):
        """添加用户画像"""
        result = memory_store.add("user", "用户叫张三")
        assert result["success"] is True
        assert "用户叫张三" in result["entries"]

    def test_add_empty_content(self, memory_store):
        """添加空内容"""
        result = memory_store.add("memory", "")
        assert result["success"] is False
        assert "不能为空" in result["error"]

    def test_add_duplicate(self, memory_store):
        """添加重复条目"""
        memory_store.add("memory", "测试记忆")
        result = memory_store.add("memory", "测试记忆")
        assert result["success"] is False
        assert "已存在" in result["error"]

    def test_add_exceed_limit(self, memory_store):
        """超出字符限制"""
        memory_store.add("memory", "a" * 50)
        result = memory_store.add("memory", "b" * 60)
        assert result["success"] is False
        assert "超出限制" in result["error"]

    def test_replace_memory(self, memory_store):
        """替换记忆条目"""
        memory_store.add("memory", "Python 3.12")
        result = memory_store.replace("memory", "Python 3.12", "Python 3.13")
        assert result["success"] is True
        assert "Python 3.13" in result["entries"]
        assert "Python 3.12" not in result["entries"]

    def test_replace_not_found(self, memory_store):
        """替换不存在的条目"""
        result = memory_store.replace("memory", "不存在", "新内容")
        assert result["success"] is False
        assert "未找到" in result["error"]

    def test_replace_multiple_matches(self, memory_store):
        """多个匹配"""
        memory_store.add("memory", "Python 3.12 项目A")
        memory_store.add("memory", "Python 3.12 项目B")
        result = memory_store.replace("memory", "Python 3.12", "Python 3.13")
        assert result["success"] is False
        assert "多个匹配" in result["error"]

    def test_replace_exceed_limit(self, memory_store):
        """替换后超出限制"""
        memory_store.add("memory", "abc")
        result = memory_store.replace("memory", "abc", "x" * 200)
        assert result["success"] is False
        assert "超出限制" in result["error"]

    def test_remove_memory(self, memory_store):
        """删除记忆条目"""
        memory_store.add("memory", "测试记忆")
        result = memory_store.remove("memory", "测试记忆")
        assert result["success"] is True
        assert memory_store.memory_entries == []

    def test_remove_not_found(self, memory_store):
        """删除不存在的条目"""
        result = memory_store.remove("memory", "不存在")
        assert result["success"] is False
        assert "未找到" in result["error"]

    def test_remove_multiple_matches(self, memory_store):
        """多个匹配"""
        memory_store.add("memory", "Python 3.12 项目A")
        memory_store.add("memory", "Python 3.12 项目B")
        result = memory_store.remove("memory", "Python 3.12")
        assert result["success"] is False
        assert "多个匹配" in result["error"]

    def test_get_text_memory_empty(self, memory_store):
        """空记忆"""
        assert memory_store.get_text("memory") == ""

    def test_get_text_memory_with_entries(self, memory_store):
        """有记忆条目"""
        memory_store.add("memory", "条目1")
        memory_store.add("memory", "条目2")
        text = memory_store.get_text("memory")
        assert "条目1" in text
        assert "条目2" in text
        assert ENTRY_DELIMITER in text

    def test_get_text_user_empty(self, memory_store):
        """空用户画像"""
        assert memory_store.get_text("user") == ""

    def test_get_text_user_with_entries(self, memory_store):
        """有用户画像"""
        memory_store.add("user", "用户叫张三")
        text = memory_store.get_text("user")
        assert "用户叫张三" in text

    def test_persistence(self, tmp_path):
        """持久化测试"""
        with patch("memory_store.get_memory_dir", return_value=tmp_path / "memories"):
            # 创建并添加条目
            store1 = MemoryStore(memory_limit=2000, user_limit=1000)
            store1.add("memory", "持久化测试")
            store1.add("user", "用户叫李四")

            # 重新加载
            store2 = MemoryStore(memory_limit=2000, user_limit=1000)
            assert "持久化测试" in store2.memory_entries
            assert "用户叫李四" in store2.user_entries
