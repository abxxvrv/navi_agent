"""Tests for navi_agent.storage.version_tracker — 覆盖任务 A 的 mtime 误报修复。"""

import os
import time
import pytest

from navi_agent.storage.safe_file import file_version
from navi_agent.storage.version_tracker import VersionTracker


class TestVersionTracker:
    def test_content_unchanged_check_passes(self, tmp_path):
        """记录后，内容不变 → check 通过。"""
        f = tmp_path / "file.txt"
        f.write_text("hello", encoding="utf-8")

        tracker = VersionTracker()
        tracker.record(f, file_version(f))

        current = file_version(f)
        assert tracker.check(f, current) is True

    def test_content_changed_check_fails(self, tmp_path):
        """内容改变 → check 失败。"""
        f = tmp_path / "file.txt"
        f.write_text("hello", encoding="utf-8")

        tracker = VersionTracker()
        tracker.record(f, file_version(f))

        f.write_text("world", encoding="utf-8")
        current = file_version(f)
        assert tracker.check(f, current) is False

    def test_mtime_only_change_check_passes(self, tmp_path):
        """只改 mtime、内容不变(sha256 相同) → check 必须通过(防误报)。

        这是任务 A 的核心回归测试。
        """
        f = tmp_path / "file.txt"
        f.write_text("same content", encoding="utf-8")

        tracker = VersionTracker()
        v1 = file_version(f)
        tracker.record(f, v1)

        # 修改 mtime 但不改内容
        new_mtime_ns = v1.mtime_ns + 1_000_000_000  # +1 秒
        os.utime(f, ns=(new_mtime_ns, new_mtime_ns))

        current = file_version(f)
        # mtime 确实变了
        assert current.mtime_ns != v1.mtime_ns
        # sha256 没变
        assert current.sha256 == v1.sha256
        # check 必须通过
        assert tracker.check(f, current) is True

    def test_unrecorded_file_check_passes(self, tmp_path):
        """从未记录 → check 通过（放行）。"""
        f = tmp_path / "unrecorded.txt"
        f.write_text("anything", encoding="utf-8")

        tracker = VersionTracker()
        current = file_version(f)
        assert tracker.check(f, current) is True

    def test_conflict_result_structure(self):
        """冲突返回值结构正确。"""
        result = VersionTracker.conflict_result("test.txt")
        assert result["ok"] is False
        assert result["error"] == "FILE_CHANGED_SINCE_READ"
        assert "path" in result
        assert result["path"] == "test.txt"

    def test_record_updates_version(self, tmp_path):
        """record 后再次 check 应该使用新版本。"""
        f = tmp_path / "file.txt"
        f.write_text("v1", encoding="utf-8")

        tracker = VersionTracker()
        tracker.record(f, file_version(f))

        # 修改文件
        f.write_text("v2", encoding="utf-8")
        v2 = file_version(f)

        # 旧版本 check 失败
        assert tracker.check(f, v2) is False

        # 重新 record
        tracker.record(f, v2)

        # 新版本 check 通过
        assert tracker.check(f, v2) is True
