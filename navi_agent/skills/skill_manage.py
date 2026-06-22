"""skill_manage — 管理技能文件（读/写/列出），仅限 skills/ 目录。"""

import re
from pathlib import Path

from ..paths import get_navi_home
from ..storage.safe_file import atomic_write_text, file_lock


class SkillManageTool:
    def __init__(self):
        # 惰性导入，避免 skill_manage ↔ tools.builtin ↔ runtime 的循环导入。
        from ..tools.builtin import PatchTool

        self.skills_dir = get_navi_home() / "skills"
        self._patcher = PatchTool(workspace=self.skills_dir)

    def __call__(
        self,
        action: str,
        name: str = "",
        content: str = "",
        old_text: str = "",
        new_text: str = "",
        replace_all: bool = False,
    ) -> dict:
        if action == "list":
            return self._list()
        elif action == "read":
            return self._read(name)
        elif action == "write":
            return self._write(name, content)
        elif action == "patch":
            return self._patch(name, old_text, new_text, replace_all)
        else:
            return {"ok": False, "error": f"未知 action: {action}，可选 list / read / write / patch"}

    def _resolve(self, name: str) -> Path:
        """把技能名解析为 skills/ 下的目录，校验不越界。"""
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?", name):
            raise ValueError(f"技能名 '{name}' 不合法，只允许小写字母、数字、连字符")
        target = (self.skills_dir / name).resolve()
        if not str(target).startswith(str(self.skills_dir.resolve())):
            raise ValueError(f"路径越界: {name}")
        return target

    def _list(self) -> dict:
        skills = []
        if self.skills_dir.is_dir():
            for d in sorted(self.skills_dir.iterdir()):
                if not d.is_dir():
                    continue
                sk = d / "SKILL.md"
                if sk.exists():
                    desc = ""
                    try:
                        text = sk.read_text(encoding="utf-8")
                        m = re.search(r"^description:\s*(.+)$", text, re.MULTILINE)
                        if m:
                            desc = m.group(1).strip().strip('"')
                    except Exception:
                        pass
                    skills.append({"name": d.name, "description": desc, "path": str(d)})
        return {"ok": True, "skills": skills}

    def _read(self, name: str) -> dict:
        try:
            target = self._resolve(name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        sk = target / "SKILL.md"
        if not sk.exists():
            return {"ok": False, "error": f"技能 '{name}' 不存在"}
        content = sk.read_text(encoding="utf-8")
        return {"ok": True, "name": name, "content": content, "path": str(sk)}

    def _write(self, name: str, content: str) -> dict:
        if not content.strip():
            return {"ok": False, "error": "content 不能为空"}
        # 基础 frontmatter 校验
        if not re.search(r"^---\s*\n", content):
            return {"ok": False, "error": "SKILL.md 必须以 YAML frontmatter（---）开头"}
        if not re.search(r"^name:\s*\S", content, re.MULTILINE):
            return {"ok": False, "error": "frontmatter 缺少必填字段: name"}
        if not re.search(r"^description:\s*\S", content, re.MULTILINE):
            return {"ok": False, "error": "frontmatter 缺少必填字段: description"}
        try:
            target = self._resolve(name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        target.mkdir(parents=True, exist_ok=True)
        sk = target / "SKILL.md"
        with file_lock(sk):
            atomic_write_text(sk, content, encoding="utf-8")
        return {"ok": True, "name": name, "path": str(sk)}

    def _patch(self, name: str, old_text: str, new_text: str, replace_all: bool = False) -> dict:
        try:
            target = self._resolve(name)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        sk = target / "SKILL.md"
        if not sk.exists():
            return {"ok": False, "error": f"技能 '{name}' 不存在"}

        original = sk.read_text(encoding="utf-8")
        # 复用 patch_file 的全部逻辑（出现次数检查、replace_all、原子写、diff）。
        # 用相对路径 + skills_dir 作 workspace，resolve_path 会把改动锁在 skills/ 内。
        result = self._patcher(
            path=f"{name}/SKILL.md",
            old_text=old_text,
            new_text=new_text,
            replace_all=replace_all,
        )
        if not result.get("ok"):
            return result

        # patch 后校验 frontmatter 仍完整，破坏则回滚（不留半坏的技能文件）。
        patched = sk.read_text(encoding="utf-8")
        if (not re.search(r"^---\s*\n", patched)
                or not re.search(r"^name:\s*\S", patched, re.MULTILINE)
                or not re.search(r"^description:\s*\S", patched, re.MULTILINE)):
            atomic_write_text(sk, original, encoding="utf-8")
            return {"ok": False, "error": "patch 后 frontmatter 不完整（已回滚，未修改文件）"}
        return result
