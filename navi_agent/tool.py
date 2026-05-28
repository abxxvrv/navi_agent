from pathlib import Path
from typing import Any
import difflib
import subprocess
import re
import time


MAX_DIFF_CHARS = 12000


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
        target = (self.workspace / path).resolve()

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
            "path": str(target),
            "items": items,
            "count": len(items),
            "truncated": len(items) >= max_items,
        }
    
# 读文件的
class ReadFileTool:
    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve() # 转成绝对路径

    def __call__(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
    ) -> dict[str, Any]:
        target = (self.workspace / path).resolve()

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

        if max_lines > 500:
            max_lines = 500

        try:
            text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "文件不是有效的 UTF-8 文本。",
                "path": path,
            }

        lines = text.splitlines() # 划分成每一行
        total_lines = len(lines)

        start_index = start_line - 1
        end_index = min(start_index + max_lines, total_lines)

        selected_lines = lines[start_index:end_index] # 可选行号

        numbered_content = "\n".join(
            f"{line_no} | {line}"
            for line_no, line in enumerate(selected_lines, start=start_line)
        )

        return {
            "ok": True,
            "path": str(target),
            "start_line": start_line,
            "end_line": end_index,
            "total_lines": total_lines,
            "content": numbered_content,
            "truncated": end_index < total_lines,
        }
    
# 写文件工具
class WriteFileTool:
    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()

    def __call__(
        self,
        path: str,
        content: str,
        mode: str = "overwrite",
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        try:
            input_path = Path(path)

            target = (self.workspace / input_path).resolve()

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

            relative_path = str(target)
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

            file_mode = "w" if mode == "overwrite" else "a"

            with open(target, file_mode, encoding=encoding) as f:
                f.write(content)

            new_content = target.read_text(encoding=encoding)
            diff = make_unified_diff(old_content, new_content, relative_path)
            added_lines, removed_lines = count_diff_lines(diff)
            diff, diff_truncated = truncate_diff(diff)

            return {
                "ok": True,
                "path": relative_path,
                "mode": mode,
                "bytes_written": len(content.encode(encoding)),
                "changed": old_content != new_content,
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
    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()

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

            target = (self.workspace / input_path).resolve()

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

            # 10. 生成 diff，方便用户检查改了什么
            relative_path = str(target)
            diff = make_unified_diff(original_text, patched_text, relative_path)
            added_lines, removed_lines = count_diff_lines(diff)

            # 11. 写回文件
            target.write_text(patched_text, encoding=encoding)

            # 12. 写完后重新读取，确认真的写成功
            verified_text = target.read_text(encoding=encoding)

            if verified_text != patched_text:
                return {
                    "ok": False,
                    "error": "补丁已写入，但校验失败。",
                    "path": path,
                }

            # 13. 避免 diff 太长
            diff, diff_truncated = truncate_diff(diff)

            return {
                "ok": True,
                "path": relative_path,
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
    def __init__(self, workspace: str = ".", skills_path: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.skills_dir = (
            Path(skills_path).resolve()
            if skills_path is not None
            else self.workspace / "skills"
        )

    def __call__(
        self,
        name: str,
        max_chars: int = 20000,
        encoding: str = "utf-8",
    ) -> dict[str, Any]:
        try:
            skill_name = name.strip()

            if not skill_name:
                return {
                    "ok": False,
                    "error": "技能名称不能为空。",
                    "name": name,
                }

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

            content = skill_file.read_text(encoding=encoding)

            truncated = False
            if max_chars > 0 and len(content) > max_chars:
                content = content[:max_chars]
                truncated = True

            resources: list[str] = []
            for child in sorted(skill_dir.iterdir()):
                if child.name == "SKILL.md":
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
                "truncated": truncated,
                "resources": resources,
            }

        except UnicodeDecodeError as exc:
            return {
                "ok": False,
                "error": f"读取技能文件失败，编码可能不是 {encoding}: {exc}",
                "name": name,
            }
        except Exception as exc:
            return {
                "ok": False,
                "error": str(exc),
                "name": name,
            }


# 加载技能工具
class LoadSkillTool:
    def __init__(
        self,
        workspace: str = ".",
        skills_dir: str = "skills",
        skills_path: str | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.skills_path = (
            Path(skills_path).resolve()
            if skills_path is not None
            else self.workspace / skills_dir
        )

    def __call__(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            return {
                "ok": False,
                "error": "技能名称不能为空。",
            }

        name = name.strip()
        input_path = Path(name)

        if input_path.is_absolute() or name in {".", ".."} or len(input_path.parts) != 1:
            return {
                "ok": False,
                "error": "无效的技能名称。",
                "name": name,
            }

        skill_path = (self.skills_path / name / "SKILL.md").resolve()

        try:
            skill_path.relative_to(self.skills_path)
        except ValueError:
            return {
                "ok": False,
                "error": "拒绝访问：技能路径位于 skills 目录外。",
                "name": name,
            }

        if not skill_path.is_file():
            return {
                "ok": False,
                "error": "未找到技能。",
                "name": name,
            }

        return {
            "ok": True,
            "skill_name": name,
            "path": str(skill_path),
            "message": "该技能会在下一次模型调用时加载到系统提示词中。",
        }
        

# 搜索当前会话历史工具
class SearchSessionHistoryTool:
    def __init__(self, session_store):
        self.session_store = session_store

    def __call__(
        self,
        query: str,
        limit: int = 5,
        include_trace: bool = False,
    ) -> dict:
        if not isinstance(query, str) or not query.strip():
            return {
                "ok": False,
                "error": "query 不能为空。",
            }

        if limit < 1:
            limit = 1

        if limit > 20:
            limit = 20

        matches = self.session_store.search(
            query=query,
            limit=limit,
            include_trace=include_trace,
        )

        searched_sources = ["index.jsonl"]

        if include_trace:
            searched_sources.append("<session_id>/events.jsonl")
        else:
            searched_sources.append("<session_id>/turns.jsonl")

        return {
            "ok": True,
            "query": query,
            "scope": "all_sessions",
            "include_trace": include_trace,
            "searched_sources": searched_sources,
            "count": len(matches),
            "matches": matches,
        }


# 终端运行工具
class RunCommandTool:
    def __init__(
        self,
        workspace: str = ".",
        default_timeout: int = 10,
        max_timeout: int = 30,
        max_output_chars: int = 8000,
    ):
        self.workspace = Path(workspace).resolve()
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars

        # 第一版先做保守限制：禁止危险命令和长期运行命令
        self.blocked_contains = [
            "rm -rf",
            "del /s",
            "rmdir /s",
            "format ",
            "shutdown",
            "reboot",
            "npm run dev",
            "python -m http.server",
            "uvicorn",
            "flask run",
            "django runserver",
            "dir /s",
        ]

        # 这些命令单独运行时通常会进入交互模式或打开 shell
        self.blocked_exact = {
            "python",
            "python.exe",
            "py",
            "node",
            "cmd",
            "cmd.exe",
            "powershell",
            "powershell.exe",
            "bash",
            "sh",
        }

        # 第一版不允许复杂 shell 拼接，降低误操作风险
        self.blocked_operators = [
            "&&",
            "||",
            ";",
            "|",
            "`",
            "$(",
        ]

    def __call__(
        self,
        command: str,
        cwd: str = ".",
        timeout_seconds: int | None = None,
        encoding: str = "utf-8",
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
            target_cwd = (self.workspace / input_cwd).resolve()

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

            # 2. 检查 timeout
            if timeout_seconds is None:
                timeout_seconds = self.default_timeout

            if timeout_seconds < 1:
                return {
                    "ok": False,
                    "error": "timeout_seconds 必须大于等于 1。",
                    "command": command,
                }

            if timeout_seconds > self.max_timeout:
                timeout_seconds = self.max_timeout

            # 3. 安全检查
            safety_error = self._check_command_safety(command)
            if safety_error is not None:
                return {
                    "ok": False,
                    "error": safety_error,
                    "command": command,
                }

            # 4. 执行命令
            started_at = time.perf_counter()
            completed = subprocess.run(
                command,
                cwd=str(target_cwd),
                shell=True,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=timeout_seconds,
            )
            duration_seconds = time.perf_counter() - started_at

            stdout, stdout_truncated = self._truncate_output(completed.stdout)
            stderr, stderr_truncated = self._truncate_output(completed.stderr)
            output = stdout
            if stderr:
                output = f"{stdout}\n{stderr}" if stdout else stderr

            return {
                "ok": completed.returncode == 0,
                "command": command,
                "cwd": str(target_cwd),
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "output": output,
                "timed_out": False,
                "timeout_seconds": timeout_seconds,
                "duration_seconds": round(duration_seconds, 3),
                "output_truncated": stdout_truncated or stderr_truncated,
            }

        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            stderr = e.stderr or ""

            if isinstance(stdout, bytes):
                stdout = stdout.decode(encoding, errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode(encoding, errors="replace")

            stdout, stdout_truncated = self._truncate_output(stdout)
            stderr, stderr_truncated = self._truncate_output(stderr)

            return {
                "ok": False,
                "command": command,
                "cwd": cwd,
                "exit_code": None,
                "stdout": stdout,
                "stderr": stderr,
                "timed_out": True,
                "timeout_seconds": timeout_seconds,
                "output_truncated": stdout_truncated or stderr_truncated,
                "error": "命令执行超时。",
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
                "command": command,
                "cwd": cwd,
            }

    def _check_command_safety(self, command: str) -> str | None:
        normalized = " ".join(command.lower().strip().split())

        if normalized in self.blocked_exact:
            return (
                "这个命令看起来是交互式命令，或者会打开 shell。"
                "只允许运行短时间、非交互式命令。"
            )

        # for operator in self.blocked_operators:
        #     if operator in command:
        #         return (
        #             f"第一版不允许使用 shell 操作符 '{operator}'。"
        #             "一次只运行一个简单命令。"
        #         )

        for blocked in self.blocked_contains:
            if blocked in normalized:
                return f"已阻止可能危险或暂不支持的命令：{blocked}"

        # 禁止用 .. 逃出工作区
        if ".." in command:
            return "命令中不允许使用 '..' 进行路径穿越。"

        # 禁止用户目录简写
        if "~" in command:
            return "命令中不允许使用用户目录简写 '~'。"

        return None

    def _truncate_output(self, text: str) -> tuple[str, bool]:
        if text is None:
            return "", False

        if len(text) <= self.max_output_chars:
            return text, False

        truncated_text = (
            text[: self.max_output_chars]
            + "\n\n... 输出已截断 ..."
        )
        return truncated_text, True
