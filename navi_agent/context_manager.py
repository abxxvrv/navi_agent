import platform
from pathlib import Path
from typing import Any

import psutil


class ContextManager:
    """
    为每次模型调用构建运行时上下文。

    AGENTS.md 和 SKILL.md 会被加载到临时 system message 中，
    不应该追加到持久化的对话 messages 里。
    """

    def __init__(
        self,
        workspace: str = ".",
        soul_filename: str = ".navi/SOUL.md",
        agents_filename: str = "AGENTS.md",
        skills_dirname: str = "skills",
        skills_path: str | None = None,
        navi_home: str | None = None,
        max_soul_chars: int = 12000,
        max_agents_chars: int = 12000,
        max_skill_chars: int = 8000,
        max_total_skill_chars: int = 20000,
    ):
        self.workspace = Path(workspace).resolve()
        self.navi_home = Path(navi_home).resolve() if navi_home is not None else None
        self.soul_path = (
            self.navi_home / "SOUL.md"
        )
        self.agents_path = self.workspace / agents_filename
        self.skills_path = (
            Path(skills_path).resolve()
            if skills_path is not None
            else self.workspace / skills_dirname
        )
        self.max_soul_chars = max_soul_chars
        self.max_agents_chars = max_agents_chars
        self.max_skill_chars = max_skill_chars
        self.max_total_skill_chars = max_total_skill_chars

    def load_soul_md(self) -> str:
        return self._read_text_file(self.soul_path, self.max_soul_chars)

    def load_agents_md(self) -> str:
        return self._read_text_file(self.agents_path, self.max_agents_chars)

    def build_environment_prompt(self) -> str:
        return "\n".join(
            [
                f"当前系统: {platform.system()}",
                f"当前工作目录: {self.workspace}",
                f"Navi home: {self.navi_home}" if self.navi_home else "",
                f"技能目录: {self.skills_path}",
                f"会话目录: {self.navi_home / 'sessions'}" if self.navi_home else "",
                f"运行终端: {self._detect_terminal()}",
            ]
        )

    def load_skill_md(self, skill_name: str) -> str:
        if not self._is_safe_skill_name(skill_name):
            return ""

        skill_path = self.skills_path / skill_name / "SKILL.md"
        return self._read_text_file(skill_path, self.max_skill_chars)

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

            content = self._read_text_file(skill_path, self.max_skill_chars)
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
        soul_md = self.load_soul_md()
        parts = [self._wrap_block("SOUL.md", soul_md)] # 注入SOUL.md

        agents_md = self.load_agents_md()
        if agents_md:
            parts.append(self._wrap_block("AGENTS.md", agents_md))# 注入AGENTS.md
        
        # skill_blocks = 已加载技能的完整正文
        # extra_instructions = 附加提示，目前主要是可用技能索引
        skill_blocks = self._load_skill_blocks(active_skills or [])
        if skill_blocks:
            parts.extend(skill_blocks)

        if extra_instructions.strip():
            parts.append(self._wrap_block("EXTRA_INSTRUCTIONS", extra_instructions.strip()))

        parts.append(self._wrap_block("ENVIRONMENT", self.build_environment_prompt()))

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
        used_chars = 0

        for skill_name in self._dedupe(active_skills):
            content = self.load_skill_md(skill_name)
            if not content:
                continue

            remaining = self.max_total_skill_chars - used_chars
            if remaining <= 0:
                break

            if len(content) > remaining:
                content = content[:remaining] + "\n\n[SKILL.md 已截断]"

            used_chars += len(content)
            blocks.append(self._wrap_block(f"SKILL name={skill_name}", content))

        return blocks

    def _read_text_file(self, path: Path, max_chars: int) -> str:
        resolved = path.resolve()

        if not resolved.exists() or not resolved.is_file():
            return ""

        try:
            text = resolved.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return ""

        if len(text) > max_chars:
            return text[:max_chars] + "\n\n[上下文文件已截断]"

        return text

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

    def _detect_terminal(self) -> str:
        try:
            parent = psutil.Process().parent()
        except psutil.Error:
            return "unknown"

        if parent is None:
            return "unknown"

        name = parent.name()
        lower_name = name.lower()
        labels = {
            "powershell.exe": "PowerShell",
            "pwsh.exe": "PowerShell",
            "cmd.exe": "Command Prompt",
            "bash.exe": "Bash",
            "zsh": "Zsh",
        }
        label = labels.get(lower_name)
        if label:
            return f"{label} ({name})"

        return name

    def _dedupe(self, items: list[str]) -> list[str]:
        seen = set()
        result = []

        for item in items:
            if item in seen:
                continue
            seen.add(item)
            result.append(item)

        return result
