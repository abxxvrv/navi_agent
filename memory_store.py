"""记忆存储 - 管理 MEMORY.md 和 USER.md"""

import os
from pathlib import Path


def get_navi_home() -> Path:
    return Path(os.environ.get("NAVI_HOME", Path.home() / ".navi")).resolve()

ENTRY_DELIMITER = "§"


def get_memory_dir() -> Path:
    return get_navi_home() / "memories"


class MemoryStore:
    def __init__(self, memory_limit: int = 2000, user_limit: int = 1000):
        self.memory_limit = memory_limit
        self.user_limit = user_limit
        self.memory_entries = self._load("MEMORY.md")
        self.user_entries = self._load("USER.md")

    def _load(self, filename: str) -> list[str]:
        path = get_memory_dir() / filename
        if not path.exists():
            return []
        content = path.read_text(encoding="utf-8")
        return [e.strip() for e in content.split(ENTRY_DELIMITER) if e.strip()]

    def _save(self, target: str):
        entries = self.user_entries if target == "user" else self.memory_entries
        filename = "USER.md" if target == "user" else "MEMORY.md"
        path = get_memory_dir() / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ENTRY_DELIMITER.join(entries), encoding="utf-8")

    def add(self, target: str, content: str) -> dict:
        content = content.strip()
        if not content:
            return {"success": False, "error": "内容不能为空"}

        entries = self.user_entries if target == "user" else self.memory_entries
        limit = self.user_limit if target == "user" else self.memory_limit

        if content in entries:
            return {"success": False, "error": "条目已存在"}

        current_len = len(ENTRY_DELIMITER.join(entries))
        if current_len + len(content) + 1 > limit:
            return {"success": False, "error": f"超出限制 ({current_len}/{limit})"}

        entries.append(content)
        self._save(target)
        return {"success": True, "entries": entries}

    def replace(self, target: str, old_text: str, new_content: str) -> dict:
        old_text = old_text.strip()
        new_content = new_content.strip()

        entries = self.user_entries if target == "user" else self.memory_entries

        matches = [i for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"未找到匹配 '{old_text}'"}
        if len(matches) > 1:
            return {"success": False, "error": "多个匹配，请更具体"}

        limit = self.user_limit if target == "user" else self.memory_limit
        test_entries = entries.copy()
        test_entries[matches[0]] = new_content
        if len(ENTRY_DELIMITER.join(test_entries)) > limit:
            return {"success": False, "error": "替换后超出限制"}

        entries[matches[0]] = new_content
        self._save(target)
        return {"success": True, "entries": entries}

    def remove(self, target: str, old_text: str) -> dict:
        old_text = old_text.strip()

        entries = self.user_entries if target == "user" else self.memory_entries

        matches = [i for i, e in enumerate(entries) if old_text in e]
        if not matches:
            return {"success": False, "error": f"未找到匹配 '{old_text}'"}
        if len(matches) > 1:
            return {"success": False, "error": "多个匹配，请更具体"}

        entries.pop(matches[0])
        self._save(target)
        return {"success": True, "entries": entries}

    def get_memory_text(self) -> str:
        if not self.memory_entries:
            return ""
        return ENTRY_DELIMITER.join(self.memory_entries)

    def get_user_text(self) -> str:
        if not self.user_entries:
            return ""
        return ENTRY_DELIMITER.join(self.user_entries)
