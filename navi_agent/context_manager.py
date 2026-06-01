import platform
import re
from datetime import datetime
from pathlib import Path
from typing import Any


class ContextManager:
    """
    为每次模型调用构建运行时上下文。

    AGENTS.md 和 SKILL.md 会被加载到临时 system message 中，
    不应该追加到持久化的对话 messages 里。
    """

    def __init__(
        self,
        workspace: str = ".",
        system_filename: str = "system.md",
        agents_filename: str = "AGENTS.md",
        skills_dirname: str = "skills",
        skills_path: str | None = None,
        navi_home: str | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.navi_home = Path(navi_home).resolve() if navi_home is not None else None
        self.system_prompt_path = (
            self.navi_home / system_filename
            if self.navi_home is not None
            else self.workspace / ".navi" / system_filename
        )
        self.agents_path = self.workspace / agents_filename
        self.skills_path = (
            Path(skills_path).resolve()
            if skills_path is not None
            else self.workspace / skills_dirname
        )

    def load_system_prompt_md(self) -> str:
        return self._read_text_file(self.system_prompt_path, None)

    def load_agents_md(self) -> str:
        return self._read_text_file(self.agents_path, None)

    def load_skill_md(self, skill_name: str) -> str:
        if not self._is_safe_skill_name(skill_name):
            return ""

        skill_path = self.skills_path / skill_name / "SKILL.md"
        return self._read_text_file(skill_path, None)

    def list_available_skills(self) -> list[str]:
        if not self.skills_path.exists() or not self.skills_path.is_dir():
            return []

        skills = []
        for item in self.skills_path.iterdir():
            if item.is_dir() and (item / "SKILL.md").is_file():
                skills.append(item.name)

        return sorted(skills)

    def scan_skill_index(self) -> list[dict[str, str]]:
        if not self.skills_path.exists() or not self.skills_path.is_dir():
            return []

        index = []

        for item in sorted(self.skills_path.iterdir(), key=lambda path: path.name):
            skill_path = item / "SKILL.md"
            if not item.is_dir() or not skill_path.is_file():
                continue

            content = self._read_text_file(skill_path, None)
            metadata = self._parse_skill_frontmatter(content)
            name = metadata.get("name") or item.name
            description = metadata.get("description", "")

            index.append(
                {
                    "name": name,
                    "description": description,
                    "path": str(skill_path),
                }
            )

        return index

    def build_skill_index_prompt(self) -> str:
        skill_index = self.scan_skill_index()

        if not skill_index:
            return ""

        lines = [
            "可用技能索引。这里仅是索引，不是完整技能正文。",
            "根据技能名称和描述判断哪些技能可能相关。",
            "只有出现在当前 SKILL 块中的技能才已经被完整加载。",
            "",
        ]

        for skill in skill_index:
            description = skill["description"] or "暂无描述。"
            lines.append(f"- {skill['name']}: {description} (路径: {skill['path']})")

        return "\n".join(lines)

    def build_system_message(
        self,
        active_skills: list[str] | None = None,
        extra_instructions: str = "",
    ) -> dict[str, str]:
        content = self.build_system_prompt(
            active_skills=active_skills,
            extra_instructions=extra_instructions,
        )

        return {
            "role": "system",
            "content": content,
        }

    def build_system_prompt(
        self,
        active_skills: list[str] | None = None,
        extra_instructions: str = "",
    ) -> str:
        agents_md = self.load_agents_md()
        system_md = self.load_system_prompt_md()
        if not system_md:
            system_md = "你是 Navi Code CLI，一个运行在用户电脑上的交互式通用 AI Agent。"

        parts = [
            self._render_system_prompt_template(
                system_md,
                agents_md=agents_md,
                skills_prompt=extra_instructions,
            )
        ]
        
        # skill_blocks = 已加载技能的完整正文
        # extra_instructions = 可用技能索引，会通过 system.md 的 ${NAVI_SKILLS} 注入
        skill_blocks = self._load_skill_blocks(active_skills or [])
        if skill_blocks:
            parts.extend(skill_blocks)

        return "\n\n".join(parts)

    def build_runtime_messages(
        self,
        messages: list[dict[str, Any]],
        active_skills: list[str] | None = None,
        extra_instructions: str = "",
    ) -> list[dict[str, Any]]:
        return [
            self.build_system_message(
                active_skills=active_skills,
                extra_instructions=extra_instructions,
            ),
            *messages,
        ]

    def _load_skill_blocks(self, active_skills: list[str]) -> list[str]:
        blocks = []

        for skill_name in self._dedupe(active_skills):
            content = self.load_skill_md(skill_name)
            if not content:
                continue

            blocks.append(self._wrap_block(f"SKILL name={skill_name}", content))

        return blocks

    def _read_text_file(self, path: Path, max_chars: int | None) -> str:
        resolved = path.resolve()

        if not resolved.exists() or not resolved.is_file():
            return ""

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ""

        if max_chars is not None and len(text) > max_chars:
            return text[:max_chars] + "\n\n[上下文文件已截断]"

        return text

    def _render_system_prompt_template(
        self,
        template: str,
        agents_md: str,
        skills_prompt: str,
    ) -> str:
        os_name = platform.system()
        additional_dirs_info = ""

        def replace_windows_block(match: re.Match[str]) -> str:
            return match.group(1) if os_name == "Windows" else ""

        def replace_additional_dirs_block(match: re.Match[str]) -> str:
            return match.group(1) if additional_dirs_info else ""

        rendered = re.sub(
            r"`?\{% if NAVI_OS == \"Windows\" %\}`?(.*?)`?\{% endif \+?%\}`?",
            replace_windows_block,
            template,
            flags=re.DOTALL,
        )
        rendered = re.sub(
            r"`?\{% if NAVI_ADDITIONAL_DIRS_INFO %\}`?(.*?)`?\{% endif %\}`?",
            replace_additional_dirs_block,
            rendered,
            flags=re.DOTALL,
        )

        replacements = {
            "${NAVI_OS}": os_name,
            "${NAVI_SHELL}": "Git Bash",
            "${NAVI_NOW}": datetime.now().isoformat(timespec="seconds"),
            "${NAVI_WORK_DIR}": str(self.workspace),
            "${NAVI_WORK_DIR_LS}": self._build_work_dir_listing(),
            "${NAVI_ADDITIONAL_DIRS_INFO}": additional_dirs_info,
            "${NAVI_AGENTS_MD}": agents_md,
            "${NAVI_SKILLS}": skills_prompt,
        }

        for placeholder, value in replacements.items():
            rendered = rendered.replace(placeholder, value)

        return rendered

    def _build_work_dir_listing(self) -> str:
        if not self.workspace.exists() or not self.workspace.is_dir():
            return ""

        lines: list[str] = []
        max_items = 50

        def add_dir(path: Path, prefix: str, depth: int) -> None:
            try:
                children = sorted(path.iterdir(), key=lambda item: item.name.lower())
            except OSError:
                return

            shown = children[:max_items]
            for child in shown:
                suffix = "/" if child.is_dir() else ""
                lines.append(f"{prefix}{child.name}{suffix}")
                if child.is_dir() and depth < 2:
                    add_dir(child, prefix + "  ", depth + 1)

            remaining = len(children) - len(shown)
            if remaining > 0:
                lines.append(f"{prefix}... and {remaining} more")

        add_dir(self.workspace, "", 1)
        return "\n".join(lines)

    def _parse_skill_frontmatter(self, content: str) -> dict[str, str]:
        metadata = {}

        if not content.startswith("---"):
            return metadata

        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            return metadata

        for line in lines[1:]:
            if line.strip() == "---":
                break

            if ":" not in line:
                continue

            key, value = line.split(":", 1)
            key = key.strip()
            value = value.strip().strip("'\"")

            if key in {"name", "description"}:
                metadata[key] = value

        return metadata

    def _is_safe_skill_name(self, skill_name: str) -> bool:
        if not skill_name or skill_name in {".", ".."}:
            return False

        path = Path(skill_name)
        return not path.is_absolute() and len(path.parts) == 1

    def _wrap_block(self, name: str, content: str) -> str:
        return f"<{name}>\n{content}\n</{name}>"

    def _dedupe(self, items: list[str]) -> list[str]:
        seen = set()
        result = []

        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)

        return result
