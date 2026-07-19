from pathlib import Path
from typing import Any
import difflib
import json
import os
import platform
import shutil
import subprocess
import re

from ..model.router import ModelRouter
from ..runtime.interrupt import is_interrupted
from ..runtime.task_manager import TaskManager
from ..runtime.tool_context import CURRENT_TOOL_CONTEXT
from ..storage.safe_file import atomic_write_text, file_lock, file_version
from ..storage.version_tracker import VersionTracker


MAX_DIFF_CHARS = 12000


def resolve_path(workspace: Path, path: str) -> Path:
    """解析路径。相对路径必须在工作区内，绝对路径直接使用。"""
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    resolved = (workspace / p).resolve()
    if not resolved.is_relative_to(workspace):
        raise ValueError("相对路径不能指向工作区外。工作区外的文件请使用绝对路径。")
    return resolved


def format_result_path(workspace: Path, path: Path | str) -> str:
    """返回工具结果路径：工作区内用相对路径，工作区外用绝对路径。"""
    resolved = Path(path).resolve()
    try:
        return str(resolved.relative_to(workspace))
    except ValueError:
        return str(resolved)


def make_unified_diff(old_text: str, new_text: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def count_diff_lines(diff: str) -> tuple[int, int]:
    added = 0
    removed = 0

    for line in diff.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1

    return added, removed


def truncate_diff(diff: str) -> tuple[str, bool]:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff, False
    return diff[:MAX_DIFF_CHARS], True

# 找文件工具
class ListDirTool:
    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve() # 变成绝对路径

    def __call__( # 将这个类像函数一样调用
        self,
        path: str = ".",
        show_hidden: bool = False,
        max_items: int = 100,
    ) -> dict[str, Any]:
        try:
            target = resolve_path(self.workspace, path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "path": path}

        if not target.exists():
            return {
                "ok": False,
                "error": "路径不存在。",
                "path": path,
            }

        if not target.is_dir():
            return {
                "ok": False,
                "error": "路径不是目录。",
                "path": path,
            }

        items = []

        for item in target.iterdir():
            if not show_hidden and item.name.startswith("."):
                continue

            if len(items) >= max_items:
                break

            items.append(
                {
                    "name": item.name,
                    "type": "directory" if item.is_dir() else "file",
                    "size": item.stat().st_size if item.is_file() else None,
                }
            )

        return {
            "ok": True,
            "path": format_result_path(self.workspace, target),
            "items": items,
            "count": len(items),
            "truncated": len(items) >= max_items,
        }

# 读文件的
class ReadFileTool:
    def __init__(self, workspace: str = ".", tracker: VersionTracker | None = None):
        self.workspace = Path(workspace).resolve() # 转成绝对路径
        self.tracker = tracker

    def __call__(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 1000,
        max_chars: int = 100 * 1024,
    ) -> dict[str, Any]:
        try:
            target = resolve_path(self.workspace, path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "path": path}

        if not target.exists():
            return {
                "ok": False,
                "error": "路径不存在。",
                "path": path,
            }

        if not target.is_file():
            return {
                "ok": False,
                "error": "路径不是文件。",
                "path": path,
            }

        # 图片文件：提示使用 vision_analyze
        _IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        if target.suffix.lower() in _IMAGE_EXTENSIONS:
            return {
                "ok": False,
                "error": (
                    f"图片文件无法以文本方式读取。"
                    f"请使用 vision_analyze 工具来分析此图片：image_path=\"{path}\""
                ),
                "path": path,
                "hint": "vision_analyze",
            }

        if start_line < 1:
            return {
                "ok": False,
                "error": "start_line 必须大于等于 1。",
                "path": path,
            }

        if max_lines < 1:
            return {
                "ok": False,
                "error": "max_lines 必须大于等于 1。",
                "path": path,
            }

        if max_lines > 1000:
            max_lines = 1000

        if max_chars < 1000:
            max_chars = 1000
        if max_chars > 100 * 1024:
            max_chars = 100 * 1024

        lines: list[str] = []
        truncated = False
        truncated_lines: list[int] = []
        current_chars = 0
        end_line: int | None = None
        try:
            with target.open(encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, start=1):
                    if i < start_line:
                        continue
                    if len(lines) >= max_lines:
                        truncated = True
                        break

                    raw = line.rstrip("\r\n")
                    if len(raw) > 2000:
                        line_text = raw[:2000] + "..."
                        truncated_lines.append(i)
                    else:
                        line_text = raw
                    rendered = f"{i} | {line_text}"
                    separator_len = 1 if lines else 0
                    remaining_chars = max_chars - current_chars

                    if separator_len + len(rendered) > remaining_chars:
                        available = remaining_chars - separator_len
                        if available > 0:
                            lines.append(rendered[:available])
                            end_line = i
                        truncated = True
                        break

                    lines.append(rendered)
                    current_chars += separator_len + len(rendered)
                    end_line = i
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": path}

        numbered_content = "\n".join(lines)

        if self.tracker is not None:
            try:
                self.tracker.record(target, file_version(target))
            except Exception:
                pass

        return {
            "ok": True,
            "path": format_result_path(self.workspace, target),
            "start_line": start_line,
            "end_line": end_line,
            "content": numbered_content,
            "truncated": truncated,
            "truncated_lines": truncated_lines,
        }

# 写文件工具
class WriteFileTool:
    def __init__(self, workspace: str = ".", tracker: VersionTracker | None = None):
        self.workspace = Path(workspace).resolve()
        self.tracker = tracker

    def __call__(
        self,
        path: str,
        content: str,
        mode: str = "overwrite",
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        try:
            input_path = Path(path)

            try:
                target = resolve_path(self.workspace, str(input_path))
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "path": path}

            if mode not in ("overwrite", "append"):
                return {
                    "ok": False,
                    "error": "mode 必须是 'overwrite' 或 'append'。",
                    "path": path,
                }

            if target.exists() and target.is_dir():
                return {
                    "ok": False,
                    "error": "目标路径是目录，不是文件。",
                    "path": path,
                }

            target.parent.mkdir(parents=True, exist_ok=True)

            result_path = format_result_path(self.workspace, target)

            with file_lock(target, should_cancel=is_interrupted):
                if self.tracker is not None:
                    # 任务 4：未读过就不许覆盖已存在的文件
                    if (
                        mode == "overwrite"
                        and target.exists()
                        and not self.tracker.has_record(target)
                    ):
                        return {
                            "ok": False,
                            "error": "MUST_READ_BEFORE_OVERWRITE",
                            "message": "请先 read_file 确认当前内容，再覆盖此文件。",
                            "path": path,
                        }

                    before = file_version(target, prev=self.tracker.get(target))
                    if not self.tracker.check(target, before):
                        return VersionTracker.conflict_result(path)

                if target.exists():
                    try:
                        old_content = target.read_text(encoding=encoding)
                    except UnicodeDecodeError:
                        return {
                            "ok": False,
                            "error": "目标文件不是指定编码下的有效文本。",
                            "path": path,
                            "encoding": encoding,
                        }
                else:
                    old_content = ""

                new_content = content if mode == "overwrite" else old_content + content
                atomic_write_text(target, new_content, encoding=encoding)

                if self.tracker is not None:
                    self.tracker.record(target, file_version(target))

            verified_content = target.read_text(encoding=encoding)
            diff = make_unified_diff(old_content, verified_content, result_path)
            added_lines, removed_lines = count_diff_lines(diff)
            diff, diff_truncated = truncate_diff(diff)

            return {
                "ok": True,
                "path": result_path,
                "mode": mode,
                "bytes_written": len(content.encode(encoding)),
                "changed": old_content != verified_content,
                "added_lines": added_lines,
                "removed_lines": removed_lines,
                "diff": diff,
                "diff_truncated": diff_truncated,
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "path": path,
            }


# 局部修改文件工具
class PatchTool:
    def __init__(self, workspace: str = ".", tracker: VersionTracker | None = None):
        self.workspace = Path(workspace).resolve()
        self.tracker = tracker

    def __call__(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        try:
            input_path = Path(path)

            try:
                target = resolve_path(self.workspace, str(input_path))
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "path": path}

            if not target.exists():
                return {
                    "ok": False,
                    "error": "路径不存在。",
                    "path": path,
                }

            if not target.is_file():
                return {
                    "ok": False,
                    "error": "路径不是文件。",
                    "path": path,
                }

            # 6. old_text 不能为空
            if old_text == "":
                return {
                    "ok": False,
                    "error": "old_text 不能为空。",
                    "path": path,
                }

            result_path = format_result_path(self.workspace, target)

            with file_lock(target, should_cancel=is_interrupted):
                if self.tracker is not None:
                    before = file_version(target, prev=self.tracker.get(target))
                    if not self.tracker.check(target, before):
                        return VersionTracker.conflict_result(path)

                # 7. 读取原文件
                try:
                    original_text = target.read_text(encoding=encoding)
                except UnicodeDecodeError:
                    return {
                        "ok": False,
                        "error": "文件不是指定编码下的有效文本。",
                        "path": path,
                        "encoding": encoding,
                    }

                # 8. 检查 old_text 出现次数
                count = original_text.count(old_text)

                if count == 0:
                    return {
                        "ok": False,
                        "error": "文件中未找到 old_text。",
                        "path": path,
                    }

                if count > 1 and not replace_all:
                    return {
                        "ok": False,
                        "error": (
                            f"old_text 出现了 {count} 次。"
                            "请提供更精确的 old_text，或设置 replace_all=True。"
                        ),
                        "path": path,
                        "matches": count,
                    }

                # 9. 替换内容
                if replace_all:
                    patched_text = original_text.replace(old_text, new_text)
                    replacements = count
                else:
                    patched_text = original_text.replace(old_text, new_text, 1)
                    replacements = 1

                # 10. 写回文件（原子写入）
                atomic_write_text(target, patched_text, encoding=encoding)

                if self.tracker is not None:
                    self.tracker.record(target, file_version(target))

            # 11. 写完后重新读取，确认真的写成功
            verified_text = target.read_text(encoding=encoding)

            if verified_text != patched_text:
                return {
                    "ok": False,
                    "error": "补丁已写入，但校验失败。",
                    "path": path,
                }

            # 12. 生成 diff
            diff = make_unified_diff(original_text, verified_text, result_path)
            added_lines, removed_lines = count_diff_lines(diff)

            # 13. 避免 diff 太长
            diff, diff_truncated = truncate_diff(diff)

            return {
                "ok": True,
                "path": result_path,
                "replacements": replacements,
                "bytes_written": len(patched_text.encode(encoding)),
                "changed": original_text != verified_text,
                "added_lines": added_lines,
                "removed_lines": removed_lines,
                "diff": diff,
                "diff_truncated": diff_truncated,
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "path": path,
            }


class SkillViewTool:
    def __init__(
        self,
        workspace: str = ".",
        skills_path: str | None = None,
        plugin_skills: dict[str, dict[str, Any]] | None = None,
        session_id: str | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.skills_dir = (
            Path(skills_path).resolve()
            if skills_path is not None
            else self.workspace / "skills"
        )
        self.plugin_skills = plugin_skills or {}
        self.session_id = session_id

    def __call__(self, name: str) -> dict[str, Any]:
        try:
            skill_name = name.strip()

            if not skill_name:
                return {
                    "ok": False,
                    "error": "技能名称不能为空。",
                    "name": name,
                }

            plugin_skill = self.plugin_skills.get(skill_name)
            if plugin_skill is not None:
                skill_file = Path(plugin_skill["path"]).resolve()
                skill_dir = skill_file.parent
                if not skill_file.is_relative_to(Path(plugin_skill["root"]).resolve()):
                    return {
                        "ok": False,
                        "error": "Access denied: skill path is outside plugin directory.",
                        "name": skill_name,
                    }
            else:
                if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", skill_name):
                    return {
                        "ok": False,
                        "error": "非法技能名称。只允许字母、数字、下划线和连字符，长度不超过 64。",
                        "name": name,
                    }

                skill_dir = (self.skills_dir / skill_name).resolve()
                skill_file = (skill_dir / "SKILL.md").resolve()

                if not skill_file.is_relative_to(self.skills_dir.resolve()):
                    return {
                        "ok": False,
                        "error": "Access denied: skill path is outside skills directory.",
                        "name": skill_name,
                    }

            if not skill_file.exists():
                return {
                    "ok": False,
                    "error": f"技能不存在：{skill_name}",
                    "name": skill_name,
                    "expected_path": str(skill_file),
                }

            if not skill_file.is_file():
                return {
                    "ok": False,
                    "error": "SKILL.md 不是普通文件。",
                    "name": skill_name,
                    "path": str(skill_file),
                }

            content = skill_file.read_text(encoding="utf-8")
            if plugin_skill is not None:
                for token, replacement in (
                    ("${GROK_PLUGIN_ROOT}", plugin_skill["root"]),
                    ("${CLAUDE_PLUGIN_ROOT}", plugin_skill["root"]),
                    ("${GROK_PLUGIN_DATA}", plugin_skill["data_dir"]),
                    ("${CLAUDE_PLUGIN_DATA}", plugin_skill["data_dir"]),
                    ("${SKILL_DIR}", skill_dir),
                    ("${CLAUDE_SKILL_DIR}", skill_dir),
                ):
                    content = content.replace(token, str(replacement))
                if self.session_id is not None:
                    content = content.replace("${SESSION_ID}", self.session_id).replace(
                        "${CLAUDE_SESSION_ID}",
                        self.session_id,
                    )

            resources: list[str] = []
            for child in sorted(skill_dir.iterdir()):
                if child == skill_file:
                    continue
                if child.is_dir():
                    resources.append(f"{child.name}/")
                else:
                    resources.append(child.name)

            return {
                "ok": True,
                "name": skill_name,
                "path": str(skill_file),
                "content": content,
                "resources": resources,
            }

        except UnicodeDecodeError as exc:
            return {
                "ok": False,
                "error": f"读取技能文件失败，编码可能不是 utf-8: {exc}",
                "name": name,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "name": name,
            }


# 终端运行工具
class RunCommandTool:
    def __init__(
        self,
        workspace: str = ".",
        default_timeout: int = 60,
        max_timeout: int = 300,
        max_output_chars: int = 50_000,
        on_output=None,
        shell: str = "bash",
        task_manager: TaskManager | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars
        self.on_output = on_output
        self.shell = shell
        self.task_manager = task_manager
        if shell == "powershell":
            self.shell_path = shutil.which("pwsh") or shutil.which("powershell")
        elif platform.system() == "Linux":
            self.shell_path = "/bin/bash"
        else:
            self.shell_path = self._resolve_bash_path()

        # 打印找到的 shell 路径
        # print(f"[navi] shell_path={self.shell_path}")

    def __call__(
        self,
        command: str,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        encoding: str = "utf-8",
        background: bool = False,
    ) -> dict[str, Any]:
        try:
            if not isinstance(command, str) or not command.strip():
                return {
                    "ok": False,
                    "error": "command 不能为空。",
                    "command": command,
                }

            command = command.strip()

            input_cwd = Path(cwd)
            try:
                target_cwd = resolve_path(self.workspace, str(input_cwd))
            except ValueError as exc:
                return {"ok": False, "error": str(exc), "cwd": cwd}

            if not target_cwd.exists():
                return {
                    "ok": False,
                    "error": "cwd 不存在。",
                    "cwd": cwd,
                    "command": command,
                }

            if not target_cwd.is_dir():
                return {
                    "ok": False,
                    "error": "cwd 不是目录。",
                    "cwd": cwd,
                    "command": command,
                }

            if self.task_manager is None:
                self.task_manager = TaskManager(max_output_chars=self.max_output_chars)
            return self._run_managed(
                command=command,
                cwd=target_cwd,
                timeout_seconds=timeout_seconds,
                encoding=encoding,
                background=background,
            )

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

    def _run_managed(
        self,
        *,
        command: str,
        cwd: Path,
        timeout_seconds: int | None,
        encoding: str,
        background: bool,
    ) -> dict[str, Any]:
        if self.shell_path is None:
            shell_label = "PowerShell" if self.shell == "powershell" else "Git Bash bash.exe"
            return {
                "ok": False,
                "error": f"未找到 {shell_label}。",
                "command": command,
                "cwd": str(cwd),
                "shell": self.shell,
            }

        if timeout_seconds is not None and timeout_seconds < 0:
            return {"ok": False, "error": "timeout_seconds 不能小于 0。", "command": command}
        if background:
            resolved_timeout = min(timeout_seconds, 36_000) if timeout_seconds else None
        else:
            resolved_timeout = timeout_seconds or self.default_timeout
            resolved_timeout = min(resolved_timeout, self.max_timeout)

        displayed_chars = 0
        cli_truncated = False

        def stream_output(text: str) -> None:
            nonlocal displayed_chars, cli_truncated
            if self.on_output is None or cli_truncated:
                return
            remaining = 2000 - displayed_chars
            if remaining > 0:
                self.on_output(text[:remaining], end="", markup=False)
                displayed_chars += min(len(text), remaining)
            if len(text) > remaining:
                self.on_output("\n... (output truncated)", end="", markup=False)
                cli_truncated = True

        context = CURRENT_TOOL_CONTEXT.get()
        snapshot = self.task_manager.start_command(
            command,
            cwd,
            shell_path=self.shell_path,
            shell=self.shell,
            background=background,
            timeout_seconds=resolved_timeout,
            encoding=encoding,
            tool_call_id=context.tool_call_id if context is not None else None,
            on_output=stream_output,
            is_cancelled=(
                context.scope.is_cancelled
                if context is not None and context.scope is not None
                else is_interrupted
            ),
        )
        result = {
            **snapshot,
            "output_truncated": snapshot.get("truncated", False),
        }
        if background or snapshot["status"] == "running":
            return {"ok": True, "backgrounded": True, **result}
        if snapshot.get("interrupted"):
            return {
                "ok": False,
                **result,
                "error": "命令执行已中断。",
                "interrupted": True,
            }
        if snapshot["status"] == "timed_out":
            return {"ok": False, **result, "error": "命令执行超时。"}
        if snapshot["status"] == "cancelled":
            return {"ok": False, **result, "error": "命令执行已取消。"}
        return {"ok": snapshot["status"] == "completed", **result}

    def _resolve_bash_path(self) -> str | None:
        env_path = os.environ.get("GIT_BASH")
        if env_path and Path(env_path).is_file():
            return env_path

        git_paths: list[Path] = []
        try:
            where_result = subprocess.run(
                ["where.exe", "git"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
            )
            if where_result.returncode == 0:
                for line in where_result.stdout.splitlines():
                    git_path = Path(line.strip())
                    if git_path.is_file():
                        git_paths.append(git_path)
        except Exception:
            pass

        for git_path in git_paths:
            git_root = git_path.parent.parent
            for candidate in (
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)

        for git_path in git_paths:
            try:
                exec_result = subprocess.run(
                    [str(git_path), "--exec-path"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                )
            except Exception:
                continue

            if exec_result.returncode != 0:
                continue

            exec_path = Path(exec_result.stdout.strip())
            if not exec_path:
                continue

            for parent in (exec_path, *exec_path.parents):
                for candidate in (
                    parent / "bin" / "bash.exe",
                    parent / "usr" / "bin" / "bash.exe",
                ):
                    if candidate.is_file():
                        return str(candidate)

        for candidate in (
            r"D:\Git\bin\bash.exe",
            r"D:\Git\usr\bin\bash.exe",
            r"C:\Program Files\Git\bin\bash.exe",
            r"C:\Program Files\Git\usr\bin\bash.exe",
            r"C:\Program Files (x86)\Git\bin\bash.exe",
        ):
            if Path(candidate).is_file():
                return candidate

        return None


class GlobTool:
    """文件名匹配工具，用 glob 模式查找文件和目录。"""

    MAX_MATCHES = 1000

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()

    def __call__(
        self,
        pattern: str,
        path: str = ".",
        include_dirs: bool = False,
        include_hidden: bool = False,
        limit: int = 100,
    ) -> dict:
        if not pattern or not pattern.strip():
            return {"ok": False, "error": "pattern 不能为空。"}

        pattern = pattern.strip()
        limit = min(limit, self.MAX_MATCHES)

        try:
            target = resolve_path(self.workspace, path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "path": path}

        if not target.exists():
            return {"ok": False, "error": f"目录不存在: {path}", "path": path}

        if not target.is_dir():
            return {"ok": False, "error": f"不是目录: {path}", "path": path}

        if pattern.startswith("**") and target == self.workspace:
            return {
                "ok": False,
                "error": "从默认工作区根目录搜索时 pattern 不能以 ** 开头，请用更具体的模式如 src/**/*.py，或通过 path 指定搜索目录。",
            }

        try:
            matches = sorted(target.glob(pattern))
        except Exception as exc:
            return {"ok": False, "error": f"无效的 glob 模式: {exc}", "pattern": pattern}

        if not include_dirs:
            matches = [p for p in matches if p.is_file()]

        if not include_hidden:
            matches = [
                p
                for p in matches
                if not any(part.startswith(".") for part in p.relative_to(target).parts)
            ]

        truncated = len(matches) > limit
        if truncated:
            matches = matches[:limit]

        files = []
        for p in matches:
            result_path = format_result_path(self.workspace, p)
            entry = {"path": result_path, "type": "directory" if p.is_dir() else "file"}
            if p.is_file():
                try:
                    entry["size"] = p.stat().st_size
                except OSError:
                    pass
            files.append(entry)

        return {
            "ok": True,
            "pattern": pattern,
            "directory": str(target),
            "files": files,
            "total": len(files),
            "truncated": truncated,
        }


class GrepTool:
    """全文搜索工具，优先用 ripgrep，fallback 到纯 Python。"""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def __call__(
        self,
        query: str,
        path: str = ".",
        glob: str = "",
        limit: int = 30,
        context_lines: int = 3,
    ) -> dict:
        if not isinstance(query, str) or not query.strip():
            return {"ok": False, "error": "query 不能为空。"}

        query = query.strip()
        limit = min(max(limit, 1), 100)
        context_lines = min(max(context_lines, 0), 5)

        try:
            target = resolve_path(self.workspace, path)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if not target.exists():
            return {"ok": False, "error": f"路径不存在: {path}"}

        if shutil.which("rg"):
            return self._search_with_rg(query, target, glob, limit, context_lines)
        return self._search_with_python(query, target, glob, limit, context_lines)

    def _search_with_rg(self, query, target, glob, limit, context_lines):
        cmd = ["rg", "--json", "-n"]
        if context_lines > 0:
            cmd += ["-C", str(context_lines)]
        if glob:
            cmd += ["-g", glob]
        cmd += [query, str(target)]

        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, cwd=str(self.workspace)
        )
        if result.returncode == 2:
            return {"ok": False, "error": result.stderr.strip()}

        matches = []
        pending_context: list[tuple[str, int, str]] = []

        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = entry.get("type")

            if etype == "context" and context_lines > 0:
                data = entry["data"]
                rel = format_result_path(self.workspace, Path(data["path"]["text"]))
                pending_context.append((rel, data["line_number"],
                                        data["lines"]["text"].rstrip("\n")))

            elif etype == "match":
                if len(matches) >= limit:
                    break
                data = entry["data"]
                rel = format_result_path(self.workspace, Path(data["path"]["text"]))
                mline = data["line_number"]
                m = {"path": rel, "line": mline,
                     "content": data["lines"]["text"].rstrip("\n")}
                if context_lines > 0:
                    before = [c for p, l, c in pending_context
                              if p == rel and l < mline]
                    m["context_before"] = before
                    m["context_after"] = []
                    pending_context = [(p, l, c) for p, l, c in pending_context
                                       if not (p == rel and l < mline)]
                matches.append(m)

            elif etype == "end" and context_lines > 0:
                data = entry.get("data", {})
                if data.get("path"):
                    rel = format_result_path(self.workspace, Path(data["path"]["text"]))
                    for m in reversed(matches):
                        if m["path"] == rel and "context_after" in m:
                            for p, l, c in pending_context:
                                if p == rel and l > m["line"]:
                                    m["context_after"].append(c)
                            break
                    pending_context = [(p, l, c) for p, l, c in pending_context
                                       if p != rel]

        if pending_context and matches:
            for p, l, c in pending_context:
                for m in reversed(matches):
                    if m["path"] == p and "context_after" in m and l > m["line"]:
                        m["context_after"].append(c)
                        break

        return {"ok": True, "query": query, "matches": matches, "total": len(matches)}

    def _search_with_python(self, query, target, glob, limit, context_lines):
        pattern = re.compile(query)
        matches = []
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv", "venv"}
        files = sorted(target.rglob(glob) if glob else target.rglob("*"))

        for fpath in files:
            if len(matches) >= limit:
                break
            if not fpath.is_file():
                continue
            if any(part in skip_dirs for part in fpath.parts):
                continue
            try:
                text = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if pattern.search(line):
                    rel = format_result_path(self.workspace, fpath)
                    entry = {"path": rel, "line": i + 1, "content": line}
                    if context_lines > 0:
                        s = max(0, i - context_lines)
                        e = min(len(lines), i + context_lines + 1)
                        entry["context_before"] = lines[s:i]
                        entry["context_after"] = lines[i + 1:e]
                    matches.append(entry)
                    if len(matches) >= limit:
                        break

        return {"ok": True, "query": query, "matches": matches, "total": len(matches)}


class TavilySearchTool:
    """基于 Tavily API 的网页搜索工具。"""

    MAX_RESULTS_LIMIT = 20
    API_URL = "https://api.tavily.com/search"

    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "")

    def __call__(
        self,
        query: str,
        search_depth: str = "basic",
        max_results: int = 5,
        include_answer: bool = True,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        if not isinstance(query, str) or not query.strip():
            return {"ok": False, "error": "query 不能为空。"}

        query = query.strip()

        if search_depth not in ("basic", "advanced"):
            return {
                "ok": False,
                "error": "search_depth 必须是 'basic' 或 'advanced'。",
                "query": query,
            }

        max_results = max(1, min(max_results, self.MAX_RESULTS_LIMIT))

        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results,
            "include_answer": include_answer,
        }

        if include_domains:
            payload["include_domains"] = include_domains
        if exclude_domains:
            payload["exclude_domains"] = exclude_domains

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.API_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            results = []
            for r in data.get("results", []):
                entry: dict[str, Any] = {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", ""),
                    "score": r.get("score"),
                }
                results.append(entry)

            return {
                "ok": True,
                "query": query,
                "answer": data.get("answer"),
                "results": results,
                "total": len(results),
                "follow_up_questions": data.get("follow_up_questions"),
            }

        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"HTTP {exc.code}: {exc.reason}",
                "detail": error_body,
                "query": query,
            }
        except urllib.error.URLError as exc:
            return {
                "ok": False,
                "error": f"网络错误: {exc.reason}",
                "query": query,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "query": query,
            }


class TavilyExtractTool:
    """基于 Tavily Extract API 的网页内容提取工具。"""

    API_URL = "https://api.tavily.com/extract"

    def __init__(self):
        self.api_key = os.environ.get("TAVILY_API_KEY", "")

    def __call__(
        self,
        url: str,
        extract_depth: str = "basic",
    ) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        if not isinstance(url, str) or not url.strip():
            return {"ok": False, "error": "URL 不能为空。"}

        url = url.strip()

        if extract_depth not in ("basic", "advanced"):
            return {"ok": False, "error": "extract_depth 必须是 'basic' 或 'advanced'。"}

        # 1. 尝试 Tavily Extract API
        result = self._extract_via_tavily(url, extract_depth)
        if result.get("ok"):
            return result

        # 2. fallback: 本地 HTTP GET + BeautifulSoup
        return self._extract_via_http(url)

    def _extract_via_tavily(self, url: str, extract_depth: str) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        payload: dict[str, Any] = {
            "api_key": self.api_key,
            "urls": [url],
            "extract_depth": extract_depth,
        }

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                self.API_URL,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            results = data.get("results", [])
            if not results:
                return {"ok": False, "error": "Tavily 未能提取到内容。", "url": url}

            r = results[0]
            raw_content = r.get("raw_content", "")

            return {
                "ok": True,
                "url": r.get("url", url),
                "content": raw_content,
                "content_length": len(raw_content),
            }

        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return {
                "ok": False,
                "error": f"Tavily HTTP {exc.code}: {exc.reason}",
                "detail": error_body,
                "url": url,
            }
        except urllib.error.URLError as exc:
            return {
                "ok": False,
                "error": f"Tavily 网络错误: {exc.reason}",
                "url": url,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Tavily 错误: {exc}",
                "url": url,
            }

    def _extract_via_http(self, url: str) -> dict[str, Any]:
        import urllib.request
        import urllib.error

        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return {"ok": False, "error": "fallback 需要 beautifulsoup4，请执行 pip install beautifulsoup4", "url": url}

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                html = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}", "url": url}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"网络错误: {exc.reason}", "url": url}
        except Exception as exc:
            return {"ok": False, "error": str(exc), "url": url}

        soup = BeautifulSoup(html, "html.parser")

        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        text = soup.get_text(separator="\n", strip=True)

        # 去除连续空行
        lines = [line for line in text.splitlines() if line.strip()]
        content = "\n".join(lines)

        if not content:
            return {"ok": False, "error": "未能提取到正文内容。", "url": url}

        return {
            "ok": True,
            "url": url,
            "content": content,
            "content_length": len(content),
        }


class VisionAnalyzeTool:
    """图片理解工具（Hermes 双路径模式）。

    - 主模型支持多模态 → 图片作为 tool result 直接返回，主模型自己看
    - 主模型不支持多模态 → 调用辅助视觉模型分析图片，返回文字描述
    """

    MIME_MAP = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }

    def __init__(self, workspace: Path, config_path: Path, session_meta: dict):
        self.workspace = workspace
        self.session_meta = session_meta
        self.config_path = config_path
        # 辅助视觉模型路由（和主模型同体系，只需 provider + model）
        vision_provider, vision_model = self._load_aux_config(config_path)
        self._vision_router = ModelRouter(config_path, vision_provider, vision_model)

    @staticmethod
    def _load_aux_config(config_path: Path) -> tuple[str, str]:
        """读取辅助视觉模型配置（全局固定，不随会话变化）。
        返回 (provider, model)。"""
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return ("mimo", "mimo-v2.5")
        aux = cfg.get("auxiliary", {}).get("vision", {})
        provider = aux.get("provider", "mimo")
        model = aux.get("model", "mimo-v2.5")
        return (provider, model)

    def _main_supports_vision(self) -> bool:
        """实时读取当前主模型是否支持多模态（反映 /model 切换）。"""
        provider = self.session_meta.get("provider", "")
        model = self.session_meta.get("model", "")
        if not provider or not model:
            # session meta 没有则 fallback 到 config.json
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
                provider = cfg.get("default_provider", "")
                model = cfg.get("default_model", "")
            except Exception:
                return False
        else:
            try:
                cfg = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                return False
        model_info = (
            cfg.get("providers", {}).get(provider, {}).get("models", {}).get(model, {})
        )
        return model_info.get("multimodal", False)

    def _load_image(self, image_path: str) -> tuple[str, str] | tuple[None, str]:
        """加载图片并返回 (data_url, error)。"""
        import base64

        if image_path.startswith(("http://", "https://")):
            import urllib.request
            try:
                req = urllib.request.Request(image_path)
                with urllib.request.urlopen(req, timeout=30) as resp:
                    raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "png" in content_type:
                    mime = "image/png"
                elif "webp" in content_type:
                    mime = "image/webp"
                elif "gif" in content_type:
                    mime = "image/gif"
                else:
                    mime = "image/jpeg"
                b64 = base64.b64encode(raw).decode("utf-8")
                return f"data:{mime};base64,{b64}", ""
            except Exception as exc:
                return None, f"下载图片失败: {exc}"
        else:
            p = Path(image_path)
            file_path = p.resolve() if p.is_absolute() else (self.workspace / p).resolve()
            if not file_path.exists():
                return None, f"文件不存在: {file_path}"
            suffix = file_path.suffix.lower()
            mime_type = self.MIME_MAP.get(suffix)
            if not mime_type:
                return None, f"不支持的图片格式: {suffix}。支持: {', '.join(self.MIME_MAP.keys())}"
            try:
                with open(file_path, "rb") as f:
                    b64 = base64.b64encode(f.read()).decode("utf-8")
                return f"data:{mime_type};base64,{b64}", ""
            except Exception as exc:
                return None, f"读取图片失败: {exc}"

    def _call_auxiliary_vision(self, image_data_url: str, prompt: str) -> dict:
        """调用辅助视觉模型分析图片，返回文字描述。"""
        if self._vision_router._provider is None:
            return {"ok": False, "error": "辅助视觉模型未配置（config.json 中 auxiliary.vision 对应的 provider/model 不匹配任何已配置的 provider）。"}

        model = self._vision_router.model_name
        client = self._vision_router.create_request_client()

        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": image_data_url}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
                max_tokens=2048,
                stream=False,
            )

            choice = response.choices[0]
            return {
                "ok": True,
                "content": choice.message.content or "",
                "model": response.model or model,
                "usage": response.usage.model_dump() if response.usage else {},
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def __call__(
        self,
        image_path: str,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        if not image_path or not image_path.strip():
            return {"ok": False, "error": "image_path 不能为空。"}

        effective_prompt = prompt or (self.config_path.parent / "vision_prompt.txt").read_text(encoding="utf-8").strip()

        image_data_url, err = self._load_image(image_path.strip())
        if err:
            return {"ok": False, "error": err}

        # 路径 1：主模型支持多模态 → 图片注入 user message，主模型直接看
        if self._main_supports_vision():
            return {
                "ok": True,
                "content": effective_prompt,
                "_multimodal": True,
                "_image_data_url": image_data_url,
            }

        # 路径 2：调用辅助视觉模型
        return self._call_auxiliary_vision(image_data_url, effective_prompt)


def _resolve_to_root(store, session_id: str) -> str:
    """沿 parent_session_id 链找到根 session。"""
    visited = set()
    cur = session_id
    while cur and cur not in visited:
        visited.add(cur)
        meta = store.get_session(cur)
        if not meta:
            break
        parent = meta.get("parent_session_id")
        if not parent:
            break
        cur = parent
    return cur


class SearchSessionTool:
    """会话搜索工具，两种模式：

    1. DISCOVERY: query → FTS5 搜索 + lineage 去重 + bookends
    2. BROWSE: 无参数 → 最近会话列表（带 recent_messages 摘要）

    查看具体会话内容请用 read_session 工具（类似 read_file）。
    """

    name = "search_session"
    description = (
        "在历史会话中查找或浏览。传 query 时，在所有历史会话中全文检索并返回命中消息及"
        "上下文片段（query 支持 FTS5 语法：关键词、引号短语、AND/OR/NOT、前缀 *）；"
        "不传 query 时，列出最近会话，每个会话附最近 3 条消息摘要供浏览。"
        "找到目标会话后，用 read_session 读取其完整消息内容。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词。支持 FTS5 语法：简单关键词、短语（用引号）、布尔（AND/OR/NOT）、前缀（*）。不传则改为浏览最近会话。"
            },
            "limit": {
                "type": "integer",
                "description": "返回结果数，默认 3，最多 10。",
                "default": 3
            },
            "sort": {
                "type": "string",
                "enum": ["newest", "oldest"],
                "description": "排序方式：newest（最新）、oldest（最早），默认按相关性"
            }
        }
    }

    def __init__(self, navi_home: Path, current_session_id: str = None):
        self.navi_home = navi_home
        self.current_session_id = current_session_id

    def __call__(
        self,
        query: str = "",
        limit: int = 3,
        sort: str = None,
    ) -> dict[str, Any]:
        try:
            from ..storage.history_store import HistoryStore

            db_path = self.navi_home / "history.sqlite3"
            if not db_path.is_file():
                return {"ok": True, "count": 0, "results": [], "message": "没有历史会话。"}

            limit = max(1, min(limit, 10))
            store = HistoryStore.for_querying(db_path)

            # DISCOVERY 模式：query
            if query and query.strip():
                return self._discover(store, query.strip(), limit, sort)

            # BROWSE 模式：无参数
            return self._browse(store, limit)

        except Exception as e:
            return {"ok": False, "error": f"搜索失败: {str(e)}"}

    def _discover(self, store, query: str, limit: int, sort: str) -> dict:
        """DISCOVERY 模式：FTS5 搜索 + lineage 去重 + bookends。"""
        raw_results = store.search_messages(
            query=query,
            limit=50,
            sort=sort,
        )

        if not raw_results:
            return {"ok": True, "query": query, "results": [], "count": 0}

        current_root = _resolve_to_root(store, self.current_session_id) if self.current_session_id else None

        # 按 lineage 去重
        seen: dict[str, dict] = {}
        for r in raw_results:
            raw_sid = r.get("session_id")
            if not raw_sid:
                continue
            resolved = _resolve_to_root(store, raw_sid)
            # 跳过当前 session lineage
            if current_root and resolved == current_root:
                continue
            if self.current_session_id and raw_sid == self.current_session_id:
                continue
            if resolved not in seen:
                r["_lineage_root"] = resolved
                seen[resolved] = r
            if len(seen) >= limit:
                break

        results = []
        for lineage_root, match_info in seen.items():
            hit_sid = match_info.get("session_id") or lineage_root
            msg_id = match_info.get("id")
            if not msg_id:
                continue

            try:
                view = store.get_anchored_view(hit_sid, msg_id, window=5, bookend=3)
            except Exception:
                continue

            entry = {
                "session_id": hit_sid,
                "matched_role": match_info.get("role"),
                "match_message_id": msg_id,
                "bookend_start": view.get("bookend_start") or [],
                "messages": [_shape_msg(m) for m in (view.get("window") or [])],
                "bookend_end": view.get("bookend_end") or [],
                "messages_before": view.get("messages_before", 0),
                "messages_after": view.get("messages_after", 0),
            }
            if lineage_root and lineage_root != hit_sid:
                entry["parent_session_id"] = lineage_root
            results.append(entry)

        return {
            "ok": True,
            "query": query,
            "results": results,
            "count": len(results),
        }

    def _browse(self, store, limit: int) -> dict:
        """BROWSE 模式：返回最近会话列表（按 updated_at 倒序，排除当前会话 lineage）。

        每个会话带 recent_messages：最新 3 条 user/assistant 消息（每条前 300 字符，旧→新）。
        """
        current_root = _resolve_to_root(store, self.current_session_id) if self.current_session_id else None

        # 多取 5 条作为过滤当前会话 lineage 的缓冲：当前会话的 root 可能落在结果前部，
        # 过滤后需要补位。+5 是保守值（实际最多过滤 1 条，因为 list 已排除子会话）。
        fetch_n = limit + 5

        with store._connect() as conn:
            rows = conn.execute(
                """SELECT session_id, created_at, updated_at, message_count
                   FROM sessions
                   WHERE parent_session_id IS NULL
                   ORDER BY updated_at DESC
                   LIMIT ?""",
                (fetch_n,),
            ).fetchall()

            sessions = [dict(r) for r in rows]
            if not sessions:
                return {"ok": True, "results": []}

            # 批量取这些 session 的最新 3 条 user/assistant 消息。窗口函数在 DB 侧就只
            # 返回每会话 3 行（rn=1 为最新），避免把长会话的全部正文拉进内存。
            sids = [s["session_id"] for s in sessions]
            placeholders = ",".join("?" * len(sids))
            msg_rows = conn.execute(
                f"""SELECT session_id, id, role, content_text, raw_json FROM (
                        SELECT session_id, id, seq, role, content_text, raw_json,
                               ROW_NUMBER() OVER (
                                   PARTITION BY session_id ORDER BY seq DESC
                               ) AS rn
                        FROM messages
                        WHERE session_id IN ({placeholders})
                          AND role IN ('user', 'assistant')
                    )
                    WHERE rn <= 3
                    ORDER BY session_id, rn""",
                sids,
            ).fetchall()

        # 按 session_id 分组，每组取最新 3 条（seq DESC 取前 3），再反转成旧→新。
        # assistant 的工具调用从 raw_json 提取成 tool_calls，使只调工具、无文本的回合
        # 也能看出做了什么；content 与每个 args 各硬切到 300（概览，不加截断标记）。
        by_session: dict[str, list[dict]] = {}
        for r in msg_rows:
            sid = r["session_id"]
            if sid not in by_session:
                by_session[sid] = []
            entry = {
                "id": r["id"],
                "role": r["role"],
                "content": (r["content_text"] or "")[:300],
            }
            if r["role"] == "assistant" and r["raw_json"]:
                try:
                    tcs = (json.loads(r["raw_json"]).get("tool_calls")) or []
                except Exception:
                    tcs = []
                calls = [
                    {"name": (tc.get("function") or {}).get("name"),
                     "args": ((tc.get("function") or {}).get("arguments") or "")[:300]}
                    for tc in tcs
                ]
                if calls:
                    entry["tool_calls"] = calls
            by_session[sid].append(entry)

        results = []
        for s in sessions:
            sid = s.get("session_id", "")
            # 跳过当前 session lineage
            if current_root and sid == current_root:
                continue
            if self.current_session_id and sid == self.current_session_id:
                continue
            recent = by_session.get(sid, [])[:3]
            recent.reverse()  # 旧→新
            results.append({
                "session_id": sid,
                "created_at": s.get("created_at"),
                "updated_at": s.get("updated_at"),
                "message_count": s.get("message_count", 0),
                "recent_messages": recent,
            })
            if len(results) >= limit:
                break

        return {
            "ok": True,
            "results": results,
        }


class ReadSessionTool:
    """读取指定会话的消息内容，类似 read_file 之于文件。

    用于查看历史会话的详细内容——search_session 定位到目标会话后，用本工具读取消息全文。
    排除 system；assistant 的工具调用从 raw_json 提取为 tool_calls（丢弃 reasoning_content）。
    content / args / tool 结果各自按 max_chars_per_message 截断。
    游标式翻页：传上次的 last_message_id 作为下次的 start_message_id。
    """

    name = "read_session"
    description = (
        "读取某个历史会话的消息原文（相当于对会话用 read_file）。"
        "先用 search_session 定位目标会话、拿到它的 session_id，再用本工具读取内容。"
        "返回 user/assistant/tool 消息（系统提示词已排除）；assistant 的工具调用以 "
        "tool_calls=[{name,args}] 体现，所以只调工具、无文本的回合也能看出做了什么。"
        "session_id 必填；不传 start_message_id 时从第一条开始读，内容多时翻页——"
        "把返回的 last_message_id 作为下次的 start_message_id 继续往后读，直到 has_more 为 false。"
        "想看某条消息（如某次完整命令输出、文件内容、工具参数）的全文：把游标对准它前一条、"
        "limit=1、并调大 max_chars_per_message。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "session_id": {
                "type": "string",
                "description": "目标会话 ID。",
            },
            "start_message_id": {
                "type": "integer",
                "description": "游标：从该消息 ID 之后开始读取（不含该条本身）。不传或 None = 从会话第一条消息开始读。",
            },
            "limit": {
                "type": "integer",
                "description": "读取的消息条数（不含 start_message_id 本身）。默认 10，范围 1-50。",
                "default": 10,
                "minimum": 1,
                "maximum": 50,
            },
            "max_chars": {
                "type": "integer",
                "description": "返回内容的总字符上限。默认 20000，范围 1000-100000。",
                "default": 20000,
                "minimum": 1000,
                "maximum": 100000,
            },
            "max_chars_per_message": {
                "type": "integer",
                "description": "单段文本字符上限，超过则截断并在该条 truncated=true。同时作用于消息 content、"
                               "每个 tool_call 的 args、以及 tool 结果——它们各自不超过此上限。默认 2000，范围 100-10000。",
                "default": 2000,
                "minimum": 100,
                "maximum": 10000,
            },
        },
        "required": ["session_id"],
    }

    def __init__(self, navi_home: Path, current_session_id: str = None):
        self.navi_home = navi_home
        self.current_session_id = current_session_id

    def __call__(
        self,
        session_id: str,
        start_message_id: int = None,
        limit: int = 10,
        max_chars: int = 20000,
        max_chars_per_message: int = 2000,
    ) -> dict[str, Any]:
        try:
            from ..storage.history_store import HistoryStore

            db_path = self.navi_home / "history.sqlite3"
            if not db_path.is_file():
                return {"ok": False, "error": "没有历史会话数据库。"}

            # 参数 clamp
            limit = max(1, min(limit, 50))
            max_chars = max(1000, min(max_chars, 100000))
            max_chars_per_message = max(100, min(max_chars_per_message, 10000))

            store = HistoryStore.for_querying(db_path)

            # 校验 session 存在
            session_meta = store.get_session(session_id)
            if not session_meta:
                return {"ok": False, "error": f"会话 {session_id} 不存在。"}

            # 拒绝在当前活跃 session lineage 内读取（内容已在上下文中）
            if self.current_session_id:
                a_root = _resolve_to_root(store, session_id)
                c_root = _resolve_to_root(store, self.current_session_id)
                if a_root and c_root and a_root == c_root:
                    return {"ok": False, "error": "该会话在当前 lineage 内，内容已在上下文中。"}

            # 查询消息：排除 system，多取 1 条用于判断 has_more
            with store._connect() as conn:
                if start_message_id is not None:
                    rows = conn.execute(
                        """SELECT id, role, content_text, raw_json
                           FROM messages
                           WHERE session_id = ? AND id > ? AND role != 'system'
                           ORDER BY id ASC
                           LIMIT ?""",
                        (session_id, start_message_id, limit + 1),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """SELECT id, role, content_text, raw_json
                           FROM messages
                           WHERE session_id = ? AND role != 'system'
                           ORDER BY id ASC
                           LIMIT ?""",
                        (session_id, limit + 1),
                    ).fetchall()

            if not rows:
                return {
                    "ok": True,
                    "session_id": session_id,
                    "has_more": False,
                    "messages": [],
                }

            # has_more 判断：多取的那条说明还有后续
            has_more = len(rows) > limit
            rows = rows[:limit]

            def _truncate(text: str) -> tuple[str, bool]:
                """单段文本按 max_chars_per_message 截断。"""
                if len(text) > max_chars_per_message:
                    return text[:max_chars_per_message] + f"\n...[truncated, 原文 {len(text)} 字符]", True
                return text, False

            def _shape(r) -> tuple[dict, int]:
                """整形一条消息，返回 (entry, 计入总预算的字符数)。

                content、每个 tool_call 的 args、tool 结果各自按 max_chars_per_message
                截断；assistant 的工具调用从 raw_json 提取，丢弃 reasoning_content。
                """
                content, truncated = _truncate(r["content_text"] or "")
                entry: dict[str, Any] = {"id": r["id"], "role": r["role"], "content": content}
                size = len(content)
                if r["role"] == "assistant" and r["raw_json"]:
                    try:
                        tcs = (json.loads(r["raw_json"]).get("tool_calls")) or []
                    except Exception:
                        tcs = []
                    calls = []
                    for tc in tcs:
                        fn = tc.get("function") or {}
                        args, t = _truncate(fn.get("arguments") or "")
                        truncated = truncated or t
                        calls.append({"name": fn.get("name"), "args": args})
                        size += len(args)
                    if calls:
                        entry["tool_calls"] = calls
                entry["truncated"] = truncated
                return entry, size

            # 逐条整形 + 总字符截断：超预算且本页已有内容则留到下一页（不半截追加）。
            current_chars = 0
            messages: list[dict[str, Any]] = []
            for r in rows:
                entry, size = _shape(r)
                if current_chars + size > max_chars and messages:
                    has_more = True
                    break
                messages.append(entry)
                current_chars += size

            result: dict[str, Any] = {
                "ok": True,
                "session_id": session_id,
                "has_more": has_more,
                "messages": messages,
            }
            if messages and has_more:
                result["last_message_id"] = messages[-1]["id"]

            return result

        except Exception as e:
            return {"ok": False, "error": f"读取失败: {str(e)}"}


def _shape_msg(m: dict) -> dict:
    """精简消息用于返回。"""
    return {
        "id": m.get("id"),
        "role": m.get("role"),
        "content": (m.get("content") or "")[:500],
    }
