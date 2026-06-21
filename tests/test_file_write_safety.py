"""集成测试 — WriteFileTool / PatchTool + 共享 VersionTracker。"""

import os
import pytest

# 先导入 runtime 以打破循环引用（runtime → builtin → runtime.interrupt → runtime/__init__）
import navi_agent.runtime.agent  # noqa: F401
from navi_agent.tools.builtin import ReadFileTool, WriteFileTool, PatchTool
from navi_agent.storage.version_tracker import VersionTracker


@pytest.fixture
def tools(tmp_path):
    """构建一组共享 tracker 的工具实例。"""
    workspace = str(tmp_path)
    tracker = VersionTracker()
    return {
        "read": ReadFileTool(workspace=workspace, tracker=tracker),
        "write": WriteFileTool(workspace=workspace, tracker=tracker),
        "patch": PatchTool(workspace=workspace, tracker=tracker),
        "workspace": tmp_path,
        "tracker": tracker,
    }


class TestWriteFileConflictDetection:
    def test_external_modify_then_write_returns_conflict(self, tools, monkeypatch):
        """read_file 记录版本后，外部修改文件内容，再 write_file → 冲突。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("original", encoding="utf-8")

        # read 记录版本
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # 外部修改
        f.write_text("externally modified", encoding="utf-8")

        # write 应该检测到冲突
        result = tools["write"](path="test.txt", content="new content")
        assert result["ok"] is False
        assert result["error"] == "FILE_CHANGED_SINCE_READ"

    def test_normal_read_write_succeeds(self, tools, monkeypatch):
        """正常 read→write 流程 ok=True，文件内容正确写入。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("original", encoding="utf-8")

        # read
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # write
        result = tools["write"](path="test.txt", content="updated content")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "updated content"

    def test_mtime_only_change_no_conflict(self, tools, monkeypatch):
        """只改 mtime（不改内容）后再写 → 不应误报，ok=True。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("content stays same", encoding="utf-8")

        # read
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # 只改 mtime
        stat = f.stat()
        new_mtime_ns = stat.st_mtime_ns + 2_000_000_000
        os.utime(f, ns=(new_mtime_ns, new_mtime_ns))

        # write 不应该误报
        result = tools["write"](path="test.txt", content="overwritten")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "overwritten"


class TestPatchConflictDetection:
    def test_external_modify_then_patch_returns_conflict(self, tools, monkeypatch):
        """read_file 记录版本后，外部修改文件内容，再 patch_file → 冲突。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        # read
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # 外部修改
        f.write_text("completely different", encoding="utf-8")

        # patch 应该检测到冲突
        result = tools["patch"](path="test.txt", old_text="hello", new_text="hi")
        assert result["ok"] is False
        assert result["error"] == "FILE_CHANGED_SINCE_READ"

    def test_normal_read_patch_succeeds(self, tools, monkeypatch):
        """正常 read→patch 流程 ok=True。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        # read
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # patch
        result = tools["patch"](path="test.txt", old_text="hello", new_text="hi")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "hi world"

    def test_mtime_only_change_no_conflict_patch(self, tools, monkeypatch):
        """只改 mtime（不改内容）后再 patch → 不应误报。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("hello world", encoding="utf-8")

        # read
        result = tools["read"](path="test.txt")
        assert result["ok"] is True

        # 只改 mtime
        stat = f.stat()
        new_mtime_ns = stat.st_mtime_ns + 2_000_000_000
        os.utime(f, ns=(new_mtime_ns, new_mtime_ns))

        # patch 不应该误报
        result = tools["patch"](path="test.txt", old_text="hello", new_text="hi")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "hi world"


class TestWriteUpdatesTracker:
    def test_write_then_write_succeeds(self, tools, monkeypatch):
        """write 后 tracker 更新，再次 write 应该成功。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "test.txt"
        f.write_text("v1", encoding="utf-8")

        # read → write → write（不需要再 read）
        tools["read"](path="test.txt")
        result = tools["write"](path="test.txt", content="v2")
        assert result["ok"] is True

        result = tools["write"](path="test.txt", content="v3")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "v3"


class TestMustReadBeforeOverwrite:
    """任务 4：没读过就不许覆盖已存在的文件。"""

    def test_existing_file_no_read_overwrite_rejected(self, tools, monkeypatch):
        """已存在文件、从没 read_file → write_file overwrite 返回 MUST_READ_BEFORE_OVERWRITE。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "existing.txt"
        f.write_text("old content", encoding="utf-8")

        result = tools["write"](path="existing.txt", content="new content")
        assert result["ok"] is False
        assert result["error"] == "MUST_READ_BEFORE_OVERWRITE"

    def test_read_then_overwrite_allowed(self, tools, monkeypatch):
        """先 read_file 再 write_file overwrite → 放行。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "existing.txt"
        f.write_text("old content", encoding="utf-8")

        result = tools["read"](path="existing.txt")
        assert result["ok"] is True

        result = tools["write"](path="existing.txt", content="new content")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "new content"

    def test_new_file_no_read_overwrite_allowed(self, tools, monkeypatch):
        """新建文件（不存在）、没读过 → write_file overwrite 放行。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))

        result = tools["write"](path="brand_new.txt", content="hello")
        assert result["ok"] is True

        ws = tools["workspace"]
        assert (ws / "brand_new.txt").read_text(encoding="utf-8") == "hello"

    def test_existing_file_no_read_append_allowed(self, tools, monkeypatch):
        """已存在文件、没读过、mode=="append" → 放行。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "appendme.txt"
        f.write_text("start", encoding="utf-8")

        result = tools["write"](path="appendme.txt", content=" end", mode="append")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "start end"

    def test_patch_not_affected_by_must_read(self, tools, monkeypatch):
        """已存在文件、没读过 → patch_file 不返回 MUST_READ_BEFORE_OVERWRITE。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "patchme.txt"
        f.write_text("hello world", encoding="utf-8")

        result = tools["patch"](path="patchme.txt", old_text="hello", new_text="hi")
        # patch 走自己的逻辑：old_text 命中则成功
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "hi world"
        # 确保不是 MUST_READ 错误
        assert result.get("error") != "MUST_READ_BEFORE_OVERWRITE"

    def test_write_records_then_second_overwrite_allowed(self, tools, monkeypatch):
        """写成功后，对同一文件第二次 overwrite → 放行（写后已 record）。"""
        monkeypatch.setenv("NAVI_HOME", str(tools["workspace"] / ".navi_home"))
        ws = tools["workspace"]
        f = ws / "twice.txt"
        f.write_text("original", encoding="utf-8")

        # 先 read 一次，让第一次 write 通过
        tools["read"](path="twice.txt")
        result = tools["write"](path="twice.txt", content="v2")
        assert result["ok"] is True

        # 第二次 overwrite（期间没再 read）→ 放行（因为写后已 record）
        result = tools["write"](path="twice.txt", content="v3")
        assert result["ok"] is True
        assert f.read_text(encoding="utf-8") == "v3"
