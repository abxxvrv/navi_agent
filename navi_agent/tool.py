from pathlib import Path
from typing import Any
import difflib
import json
import os
import shutil
import subprocess
import re


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

        return {
            "ok": True,
            "path": str(target),
            "start_line": start_line,
            "end_line": end_line,
            "content": numbered_content,
            "truncated": truncated,
            "truncated_lines": truncated_lines,
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

    def __call__(self, name: str) -> dict[str, Any]:
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

            content = skill_file.read_text(encoding="utf-8")

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
    ):
        self.workspace = Path(workspace).resolve()
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars
        self.bash_path = self._resolve_bash_path()

        # 打印找到的 git bash 路径
        # print(f"[navi] run_command bash_path={self.bash_path}")

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

            # 3. 执行命令
            if self.bash_path is None:
                return {
                    "ok": False,
                    "error": "未找到 Git Bash bash.exe，请安装 Git for Windows 或设置 GIT_BASH。",
                    "command": command,
                    "cwd": str(target_cwd),
                    "shell": "git-bash",
                }

            completed = subprocess.run(
                [self.bash_path, "-lc", command],
                cwd=str(target_cwd),
                shell=False,
                capture_output=True,
                text=True,
                encoding=encoding,
                errors="replace",
                timeout=timeout_seconds,
            )

            stdout, stdout_truncated = self._truncate_output(completed.stdout)
            stderr, stderr_truncated = self._truncate_output(completed.stderr)
            output = stdout
            if stderr:
                output = f"{stdout}\n{stderr}" if stdout else stderr

            return {
                "ok": completed.returncode == 0,
                "exit_code": completed.returncode,
                "output": output,
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
            output = stdout
            if stderr:
                output = f"{stdout}\n{stderr}" if stdout else stderr

            return {
                "ok": False,
                "exit_code": None,
                "output": output,
                "output_truncated": stdout_truncated or stderr_truncated,
                "error": "命令执行超时。",
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

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



class GlobTool:
    """文件名匹配工具，用 glob 模式查找文件和目录。"""

    MAX_MATCHES = 1000

    def __init__(self, workspace: str = "."):
        self.workspace = Path(workspace).resolve()

    def __call__(
        self,
        pattern: str,
        directory: str = ".",
        include_dirs: bool = True,
    ) -> dict:
        if not pattern or not pattern.strip():
            return {"ok": False, "error": "pattern 不能为空。"}

        pattern = pattern.strip()

        if pattern.startswith("**"):
            return {
                "ok": False,
                "error": "pattern 不能以 ** 开头，请用更具体的模式如 src/**/*.py。",
            }

        try:
            target = resolve_path(self.workspace, directory)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "directory": directory}

        if not target.exists():
            return {"ok": False, "error": f"目录不存在: {directory}", "directory": directory}

        if not target.is_dir():
            return {"ok": False, "error": f"不是目录: {directory}", "directory": directory}

        try:
            matches = sorted(target.glob(pattern))
        except Exception as exc:
            return {"ok": False, "error": f"无效的 glob 模式: {exc}", "pattern": pattern}

        if not include_dirs:
            matches = [p for p in matches if p.is_file()]

        truncated = len(matches) > self.MAX_MATCHES
        if truncated:
            matches = matches[: self.MAX_MATCHES]

        files = []
        for p in matches:
            rel = str(p.relative_to(self.workspace))
            entry = {"path": rel, "type": "directory" if p.is_dir() else "file"}
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


class SearchFilesTool:
    """全文搜索工具，优先用 ripgrep，fallback 到纯 Python。"""

    def __init__(self, workspace: str):
        self.workspace = Path(workspace).resolve()

    def __call__(
        self,
        query: str,
        path: str = ".",
        glob: str = "",
        limit: int = 30,
        context_lines: int = 0,
    ) -> dict:
        target = (self.workspace / path).resolve()
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
                rel = str(Path(data["path"]["text"]).relative_to(self.workspace))
                pending_context.append((rel, data["line_number"],
                                        data["lines"]["text"].rstrip("\n")))

            elif etype == "match":
                if len(matches) >= limit:
                    break
                data = entry["data"]
                rel = str(Path(data["path"]["text"]).relative_to(self.workspace))
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
                    rel = str(Path(data["path"]["text"]).relative_to(self.workspace))
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
                    rel = str(fpath.relative_to(self.workspace))
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
