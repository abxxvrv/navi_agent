"""记忆存储 - 管理 MEMORY.md 和 USER.md"""

import os
from pathlib import Path

from .safe_file import atomic_write_text, file_lock


def get_navi_home() -> Path:
    return Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()


ENTRY_DELIMITER = "\n§\n"


def get_memory_dir() -> Path:
    return get_navi_home() / "memories"


class MemoryStore:
    def __init__(self, memory_limit: int = 2000, user_limit: int = 1000):
        self.memory_limit = memory_limit
        self.user_limit = user_limit
        self.memory_entries = self._load("MEMORY.md")
        self.user_entries = self._load("USER.md")

    def _target_info(self, target: str) -> tuple[str, int]:
        if target == "user":
            return "USER.md", self.user_limit
        return "MEMORY.md", self.memory_limit

    def _load(self, filename: str) -> list[str]:
        path = get_memory_dir() / filename
        if not path.exists():
            return []
        content = path.read_text(encoding="utf-8")
        return [e.strip() for e in content.split(ENTRY_DELIMITER) if e.strip()]

    def _cache_entries(self, target: str, entries: list[str]) -> None:
        if target == "user":
            self.user_entries = entries
        else:
            self.memory_entries = entries

    def _save_locked(self, target: str, entries: list[str]) -> None:
        filename, _ = self._target_info(target)
        path = get_memory_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, ENTRY_DELIMITER.join(entries), encoding="utf-8")
        self._cache_entries(target, entries)

    def _save(self, target: str):
        entries = self.user_entries if target == "user" else self.memory_entries
        filename, _ = self._target_info(target)
        path = get_memory_dir() / filename
        with file_lock(path):
            self._save_locked(target, entries)

    def add(self, target: str, content: str) -> dict:
        content = content.strip()
        if not content:
            return {"success": False, "error": "内容不能为空"}

        filename, limit = self._target_info(target)
        path = get_memory_dir() / filename

        with file_lock(path):
            entries = self._load(filename)

            if content in entries:
                self._cache_entries(target, entries)
                return {"success": False, "error": "条目已存在"}

            current_len = len(ENTRY_DELIMITER.join(entries))
            if current_len + len(content) + 1 > limit:
                self._cache_entries(target, entries)
                return {"success": False, "error": f"超出限制 ({current_len}/{limit})"}

            entries.append(content)
            self._save_locked(target, entries)
            return {"success": True, "entries": entries}

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        old_text = old_text.strip()
        new_content = new_content.strip()

        filename, limit = self._target_info(target)
        path = get_memory_dir() / filename

        with file_lock(path):
            entries = self._load(filename)

            matches = [i for i, e in enumerate(entries) if old_text in e]
            if not matches:
                self._cache_entries(target, entries)
                return {"success": False, "error": f"未找到匹配 '{old_text}'"}
            if len(matches) > 1:
                self._cache_entries(target, entries)
                return {"success": False, "error": "多个匹配，请更具体"}

            test_entries = entries.copy()
            test_entries[matches[0]] = new_content
            if len(ENTRY_DELIMITER.join(test_entries)) > limit:
                self._cache_entries(target, entries)
                return {"success": False, "error": "替换后超出限制"}

            entries[matches[0]] = new_content
            self._save_locked(target, entries)
            return {"success": True, "entries": entries}

    def remove(self, target: str, old_text: str) -> dict:
        old_text = old_text.strip()

        filename, _ = self._target_info(target)
        path = get_memory_dir() / filename

        with file_lock(path):
            entries = self._load(filename)

            matches = [i for i, e in enumerate(entries) if old_text in e]
            if not matches:
                self._cache_entries(target, entries)
                return {"success": False, "error": f"未找到匹配 '{old_text}'"}
            if len(matches) > 1:
                self._cache_entries(target, entries)
                return {"success": False, "error": "多个匹配，请更具体"}

            entries.pop(matches[0])
            self._save_locked(target, entries)
            return {"success": True, "entries": entries}

    def get_text(self, target: str) -> str:
        filename, _ = self._target_info(target)
        path = get_memory_dir() / filename
        with file_lock(path):
            entries = self._load(filename)
            self._cache_entries(target, entries)
        if not entries:
            return ""
        return ENTRY_DELIMITER.join(entries)
