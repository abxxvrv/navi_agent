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
