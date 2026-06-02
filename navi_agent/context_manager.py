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

    # 加载系统提示词.md
    def load_system_prompt_md(self) -> str:
        return self._read_text_file(self.system_prompt_path, None)

    # 加载 AGENTS.md
    def load_agents_md(self) -> str:
        return self._read_text_file(self.agents_path, None)

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

    # 构造运行时候信息，llm code 先走到这
    def build_runtime_messages(
        self,
        messages: list[dict[str, Any]],
        extra_instructions: str = "",
    ) -> list[dict[str, Any]]:
        agents_md = self.load_agents_md() # 加载 AGENTS.md
        system_md = self.load_system_prompt_md() # 加载系统提示词.md
        if not system_md:
            system_md = "你是 Navi Code CLI，一个运行在用户电脑上的交互式通用 AI Agent。"

        system_content = self._render_system_prompt_template( # 构造系统提示词
            system_md,
            agents_md=agents_md,
            skills_prompt=extra_instructions,
        )
        return [
            {
                "role": "system",
                "content": system_content,
            },
            *messages,
        ]

    # 读文件工具函数
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

    # 模板渲染函数
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
            "${NAVI_HOME}": str(self.navi_home),
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
