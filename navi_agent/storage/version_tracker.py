"""版本追踪器 — 记录文件读取时的版本，写入前校验是否被外部修改。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .safe_file import FileVersion


class VersionTracker:
    """跨工具共享的文件版本追踪器。

    ReadFileTool 读取成功后调用 record() 记录版本；
    WriteFileTool / PatchTool 写入前调用 check() 校验版本。
    """

    def __init__(self) -> None:
        self._versions: dict[str, FileVersion] = {}

    def record(self, target: Path, version: FileVersion) -> None:
        """记录文件版本。"""
        self._versions[str(target.resolve())] = version

    def has_record(self, target: Path) -> bool:
        """该路径是否有记录（被 read_file 或 write 后 record 过）。"""
        return str(target.resolve()) in self._versions

    def get(self, target: Path) -> FileVersion | None:
        """取出该路径上次记录的 FileVersion，无记录返回 None。"""
        return self._versions.get(str(target.resolve()))

    def check(self, target: Path, current: FileVersion) -> bool:
        """检查文件是否与上次记录一致。

        只比较内容标识（exists / sha256 / size），忽略 mtime_ns。
        如果从未记录过，返回 True（放行）。
        """
        key = str(target.resolve())
        expected = self._versions.get(key)
        if expected is None:
            return True
        return (
            expected.exists == current.exists
            and expected.sha256 == current.sha256
            and expected.size == current.size
        )

    @staticmethod
    def conflict_result(path: str) -> dict[str, Any]:
        """生成冲突返回值。"""
        return {
            "ok": False,
            "error": "FILE_CHANGED_SINCE_READ",
            "message": "文件自上次读取后已被外部修改，请重新 read_file 确认最新内容再修改。",
            "path": path,
        }
