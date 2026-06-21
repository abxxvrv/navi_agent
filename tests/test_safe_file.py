"""Tests for navi_agent.storage.safe_file"""

import os
import pytest

from navi_agent.storage.safe_file import atomic_write_text, file_version, file_lock


class TestAtomicWriteText:
    def test_creates_new_file(self, tmp_path):
        target = tmp_path / "new.txt"
        atomic_write_text(target, "hello world")
        assert target.read_text(encoding="utf-8") == "hello world"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "existing.txt"
        target.write_text("old content", encoding="utf-8")
        atomic_write_text(target, "new content")
        assert target.read_text(encoding="utf-8") == "new content"

    def test_creates_parent_directories(self, tmp_path):
        target = tmp_path / "a" / "b" / "c.txt"
        atomic_write_text(target, "deep")
        assert target.read_text(encoding="utf-8") == "deep"

    def test_custom_encoding(self, tmp_path):
        target = tmp_path / "gbk.txt"
        content = "你好世界"
        atomic_write_text(target, content, encoding="gbk")
        assert target.read_text(encoding="gbk") == content


class TestFileVersion:
    def test_same_content_same_sha256(self, tmp_path):
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("same content", encoding="utf-8")
        b.write_text("same content", encoding="utf-8")
        va = file_version(a)
        vb = file_version(b)
        assert va.sha256 == vb.sha256
        assert va.exists is True
        assert vb.exists is True

    def test_different_content_different_sha256(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("version 1", encoding="utf-8")
        v1 = file_version(f)
        f.write_text("version 2", encoding="utf-8")
        v2 = file_version(f)
        assert v1.sha256 != v2.sha256

    def test_nonexistent_file(self, tmp_path):
        f = tmp_path / "nonexistent.txt"
        v = file_version(f)
        assert v.exists is False
        assert v.sha256 == ""
        assert v.mtime_ns == 0
        assert v.size == 0

    def test_size_field(self, tmp_path):
        f = tmp_path / "sized.txt"
        f.write_text("abc", encoding="utf-8")
        v = file_version(f)
        assert v.size == 3


class TestFileVersionFastPath:
    """任务 5：file_version 增加 mtime+size 快路径复用哈希。"""

    def test_prev_matching_reuses_sha256(self, tmp_path):
        """prev 的 mtime_ns/size 与磁盘文件一致但 sha256 故意错误 → 原样返回 prev。"""
        from navi_agent.storage.safe_file import FileVersion

        f = tmp_path / "fast.txt"
        f.write_text("some content", encoding="utf-8")

        real = file_version(f)
        fake_prev = FileVersion(
            exists=True,
            sha256="fake_hash_not_real",
            mtime_ns=real.mtime_ns,
            size=real.size,
        )

        result = file_version(f, prev=fake_prev)
        # 快路径：应直接返回 prev 对象
        assert result is fake_prev
        assert result.sha256 == "fake_hash_not_real"

    def test_prev_size_mismatch_recomputes(self, tmp_path):
        """prev 的 size 不一致 → 重新计算，返回真实 sha256。"""
        from navi_agent.storage.safe_file import FileVersion

        f = tmp_path / "recompute.txt"
        f.write_text("content", encoding="utf-8")

        real = file_version(f)
        bad_prev = FileVersion(
            exists=True,
            sha256="stale_hash",
            mtime_ns=real.mtime_ns,
            size=real.size + 999,  # size 不一致
        )

        result = file_version(f, prev=bad_prev)
        assert result.sha256 == real.sha256
        assert result.sha256 != "stale_hash"

    def test_prev_none_full_compute(self, tmp_path):
        """prev=None → 正常全量计算。"""
        f = tmp_path / "full.txt"
        f.write_text("hello", encoding="utf-8")

        v1 = file_version(f, prev=None)
        v2 = file_version(f)
        assert v1.sha256 == v2.sha256
        assert v1.exists is True

    def test_read_no_change_write_integration(self, tmp_path, monkeypatch):
        """集成测试：read→(不改动)→write 验证流程正常。"""
        monkeypatch.setenv("NAVI_HOME", str(tmp_path / "navi_home"))

        # 先导入以打破循环引用
        import navi_agent.runtime.agent  # noqa: F401
        from navi_agent.tools.builtin import ReadFileTool, WriteFileTool
        from navi_agent.storage.version_tracker import VersionTracker

        workspace = str(tmp_path)
        tracker = VersionTracker()
        read_tool = ReadFileTool(workspace=workspace, tracker=tracker)
        write_tool = WriteFileTool(workspace=workspace, tracker=tracker)

        f = tmp_path / "integ.txt"
        f.write_text("unchanged", encoding="utf-8")

        # read
        result = read_tool(path="integ.txt")
        assert result["ok"] is True

        # write（内容相同，不改动文件）
        result = write_tool(path="integ.txt", content="unchanged")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "unchanged"


class TestFileLock:
    def test_lock_creates_and_releases(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NAVI_HOME", str(tmp_path / "navi_home"))
        target = tmp_path / "locktest.txt"
        target.write_text("x", encoding="utf-8")

        from navi_agent.storage.safe_file import _lock_path
        lp = _lock_path(target)

        with file_lock(target):
            assert lp.exists(), "Lock file should exist while lock is held"

        assert not lp.exists(), "Lock file should be removed after release"

    def test_lock_reentrance_via_separate_targets(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NAVI_HOME", str(tmp_path / "navi_home"))
        a = tmp_path / "a.txt"
        b = tmp_path / "b.txt"
        a.write_text("a", encoding="utf-8")
        b.write_text("b", encoding="utf-8")

        # Locking different targets should not deadlock
        with file_lock(a):
            with file_lock(b):
                pass
