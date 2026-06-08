from pathlib import Path
from typing import Any
import difflib
import json
import os
import platform
import signal
import shutil
import subprocess
import threading
import time
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

            result_path = format_result_path(self.workspace, target)
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
            diff = make_unified_diff(old_content, new_content, result_path)
            added_lines, removed_lines = count_diff_lines(diff)
            diff, diff_truncated = truncate_diff(diff)

            return {
                "ok": True,
                "path": result_path,
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
            result_path = format_result_path(self.workspace, target)
            diff = make_unified_diff(original_text, patched_text, result_path)
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
        on_output=None,
    ):
        self.workspace = Path(workspace).resolve()
        self.default_timeout = default_timeout
        self.max_timeout = max_timeout
        self.max_output_chars = max_output_chars
        self.on_output = on_output
        if platform.system() == "Linux":
            self.bash_path = "/bin/bash"
        else:
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

            popen_kwargs = dict(
                cwd=str(target_cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            if platform.system() == "Windows":
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                popen_kwargs["start_new_session"] = True

            proc = subprocess.Popen(
                [self.bash_path, "-lc", command],
                **popen_kwargs,
            )

            timed_out = False
            output_parts: list[str] = []

            def _reader():
                for line in proc.stdout:
                    text = line.decode(encoding, errors="replace")
                    output_parts.append(text)
                    if self.on_output:
                        self.on_output(text, end="", markup=False)
                proc.stdout.close()

            reader = threading.Thread(target=_reader)
            reader.daemon = True
            reader.start()

            deadline = time.monotonic() + timeout_seconds
            while proc.poll() is None:
                if time.monotonic() >= deadline:
                    timed_out = True
                    self._kill_process_tree(proc)
                    break
                time.sleep(0.05)

            reader.join(timeout=3)
            output, output_truncated = self._truncate_output("".join(output_parts))

            if timed_out:
                return {
                    "ok": False,
                    "exit_code": None,
                    "output": output,
                    "output_truncated": output_truncated,
                    "error": "命令执行超时。",
                }

            return {
                "ok": proc.returncode == 0,
                "exit_code": proc.returncode,
                "output": output,
                "output_truncated": output_truncated,
            }

        except Exception as e:
            return {
                "ok": False,
                "error": str(e),
            }

    def _kill_process_tree(self, proc: subprocess.Popen) -> None:
        """杀掉 proc 及其所有子进程。"""
        if platform.system() == "Windows":
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            except Exception:
                proc.kill()
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.kill()

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
        # 辅助视觉配置只需读一次（不随会话变化）
        self.aux_config = self._load_aux_config(config_path)

    @staticmethod
    def _load_aux_config(config_path: Path) -> dict:
        """读取辅助视觉模型配置（全局固定，不随会话变化）。"""
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        aux = cfg.get("auxiliary", {}).get("vision", {})
        if not aux.get("base_url"):
            mimo = cfg.get("providers", {}).get("mimo", {})
            aux.setdefault("base_url", mimo.get("base_url", ""))
            aux.setdefault("api_key", mimo.get("api_key", ""))
        if not aux.get("model"):
            aux["model"] = "mimo-v2.5"
        return aux

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
        import urllib.request
        import urllib.error

        base_url = self.aux_config.get("base_url", "").rstrip("/")
        api_key = self.aux_config.get("api_key", "")
        model = self.aux_config.get("model", "mimo-v2.5")

        if not base_url or not api_key:
            return {"ok": False, "error": "辅助视觉模型未配置（config.json 中 auxiliary.vision 或 providers.mimo）。"}

        # 拼接 prompt（Hermes 模式）
        if prompt and prompt.strip() != "请描述这张图片的内容":
            full_prompt = (
                "Fully describe and explain everything about this image, "
                "then answer the following question:\n\n" + prompt
            )
        else:
            full_prompt = (
                "Describe this image in detail. Include all visible text, "
                "objects, layout, colors, and any other relevant information."
            )

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": full_prompt},
                    ],
                }
            ],
            "max_completion_tokens": 2048,
        }

        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{base_url}/chat/completions",
                data=body,
                headers={"Content-Type": "application/json", "api-key": api_key},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            choices = data.get("choices", [])
            if not choices:
                return {"ok": False, "error": "辅助视觉模型未返回结果。", "raw": data}

            return {
                "ok": True,
                "content": choices[0].get("message", {}).get("content", ""),
                "model": data.get("model", model),
                "usage": data.get("usage", {}),
            }
        except urllib.error.HTTPError as exc:
            error_body = ""
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {exc.code}: {exc.reason}", "detail": error_body}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": f"网络错误: {exc.reason}"}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def __call__(
        self,
        image_path: str,
        prompt: str = "请描述这张图片的内容",
    ) -> dict[str, Any]:
        if not image_path or not image_path.strip():
            return {"ok": False, "error": "image_path 不能为空。"}

        image_data_url, err = self._load_image(image_path.strip())
        if err:
            return {"ok": False, "error": err}

        # 路径 1：主模型支持多模态 → 图片注入 user message，主模型直接看
        if self._main_supports_vision():
            return {
                "ok": True,
                "content": prompt,
                "_multimodal": True,
                "_image_data_url": image_data_url,
            }

        # 路径 2：调用辅助视觉模型
        return self._call_auxiliary_vision(image_data_url, prompt)
