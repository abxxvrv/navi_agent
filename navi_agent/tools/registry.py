from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
import difflib

from ..storage.safe_file import atomic_write_text, file_lock, file_version


MAX_DIFF_CHARS = 12000


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    function: Callable[..., Any]
    visible: bool = True


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {} # 默认创建一个字典 _tools = {}
        self._read_versions: dict[str, dict[str, Any]] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        function: Callable[..., Any],
        visible: bool = True,
    ):
        if name in self._tools: # 防止重复注册
            raise ValueError(f"Tool already registered: {name}")

        self._tools[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            function=function,
            visible=visible,
        )

    def unregister(self, name: str) -> bool:
        """移除已注册的工具。返回是否成功。"""
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def has(self, name: str) -> bool:
        """检查工具是否已注册。"""
        return name in self._tools

    def remove_by_prefix(self, prefix: str) -> int:
        """移除所有以 prefix 开头的工具（用于 MCP server 重载）。

        返回移除的数量。
        """
        to_remove = [n for n in self._tools if n.startswith(prefix)]
        for name in to_remove:
            del self._tools[name]
        return len(to_remove)

    def to_openai_tools(self) -> list[dict]:
        """
        转成 OpenAI / DeepSeek chat.completions.create 需要的 tools 格式。
        只包含 visible=True 的工具。
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self._tools.values()
            if tool.visible
        ]

    def invoke(self, name: str, arguments: dict) -> Any:
        """
        根据工具名执行真正的 Python 函数。
        """
        if name not in self._tools:
            raise ValueError(f"Unknown tool: {name}")

        tool = self._tools[name]

        if name == "read_file":
            result = tool.function(**arguments)
            self._remember_read_version(tool, arguments, result)
            return result

        if name in {"write_file", "patch_file"}:
            return self._invoke_file_write(tool, arguments)

        return tool.function(**arguments)

    def _resolve_tool_path(self, tool: ToolSpec, path: str) -> Path:
        raw = Path(path)
        if raw.is_absolute():
            return raw.resolve()

        workspace = getattr(tool.function, "workspace", None)
        if workspace is None:
            return raw.resolve()

        workspace_path = Path(workspace).resolve()
        resolved = (workspace_path / raw).resolve()
        if not resolved.is_relative_to(workspace_path):
            raise ValueError("相对路径不能指向工作区外。工作区外的文件请使用绝对路径。")
        return resolved

    def _result_display_path(self, tool: ToolSpec, target: Path) -> str:
        workspace = getattr(tool.function, "workspace", None)
        if workspace is None:
            return str(target.resolve())
        try:
            return str(target.resolve().relative_to(Path(workspace).resolve()))
        except ValueError:
            return str(target.resolve())

    def _remember_read_version(self, tool: ToolSpec, arguments: dict, result: Any) -> None:
        if not isinstance(result, dict) or not result.get("ok") or not arguments.get("path"):
            return
        try:
            path = self._resolve_tool_path(tool, str(arguments["path"]))
            version = file_version(path).to_dict()
        except Exception:
            return
        result["version"] = version
        self._read_versions[str(path)] = version

    def _invoke_file_write(self, tool: ToolSpec, arguments: dict) -> Any:
        path_arg = arguments.get("path")
        if not path_arg:
            return tool.function(**arguments)

        try:
            target = self._resolve_tool_path(tool, str(path_arg))
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "path": path_arg}

        with file_lock(target):
            before = file_version(target).to_dict()
            last_read = self._read_versions.get(str(target))
            if last_read is not None and before != last_read:
                return {
                    "ok": False,
                    "error": "FILE_CHANGED_SINCE_READ",
                    "message": "文件已被其他会话或外部程序修改。请重新 read_file 后再修改。",
                    "path": str(path_arg),
                    "last_read_version": last_read,
                    "current_version": before,
                }

            if tool.name == "write_file":
                result = self._safe_write_file(tool, arguments, target, before)
            else:
                result = self._safe_patch_file(tool, arguments, target, before)

            if isinstance(result, dict) and result.get("ok"):
                after = file_version(target).to_dict()
                result["version"] = after
                result["previous_version"] = before
                self._read_versions[str(target)] = after
            return result

    def _safe_write_file(
        self,
        tool: ToolSpec,
        arguments: dict,
        target: Path,
        before: dict[str, Any],
    ) -> Any:
        mode = arguments.get("mode", "overwrite")
        encoding = arguments.get("encoding", "utf-8")
        content = str(arguments.get("content", ""))
        result_path = self._result_display_path(tool, target)

        if mode not in ("overwrite", "append"):
            return {"ok": False, "error": "mode 必须是 'overwrite' 或 'append'。", "path": arguments.get("path")}

        if target.exists() and target.is_dir():
            return {"ok": False, "error": "目标路径是目录，不是文件。", "path": arguments.get("path")}

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            old_text = target.read_text(encoding=encoding) if target.exists() else ""
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "目标文件不是指定编码下的有效文本。",
                "path": arguments.get("path"),
                "encoding": encoding,
            }

        new_text = content if mode == "overwrite" else old_text + content
        atomic_write_text(target, new_text, encoding=encoding)
        verified_text = target.read_text(encoding=encoding)

        diff = _make_unified_diff(old_text, verified_text, result_path)
        added_lines, removed_lines = _count_diff_lines(diff)
        diff, diff_truncated = _truncate_diff(diff)

        return {
            "ok": True,
            "path": result_path,
            "mode": mode,
            "bytes_written": len(content.encode(encoding)),
            "changed": old_text != verified_text,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "diff": diff,
            "diff_truncated": diff_truncated,
            "previous_version": before,
        }

    def _safe_patch_file(
        self,
        tool: ToolSpec,
        arguments: dict,
        target: Path,
        before: dict[str, Any],
    ) -> Any:
        encoding = arguments.get("encoding", "utf-8")
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        replace_all = bool(arguments.get("replace_all", False))
        result_path = self._result_display_path(tool, target)

        if not target.exists():
            return {"ok": False, "error": "路径不存在。", "path": arguments.get("path")}
        if not target.is_file():
            return {"ok": False, "error": "路径不是文件。", "path": arguments.get("path")}
        if old_text == "":
            return {"ok": False, "error": "old_text 不能为空。", "path": arguments.get("path")}

        try:
            original_text = target.read_text(encoding=encoding)
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "文件不是指定编码下的有效文本。",
                "path": arguments.get("path"),
                "encoding": encoding,
            }

        count = original_text.count(old_text)
        if count == 0:
            return {"ok": False, "error": "文件中未找到 old_text。", "path": arguments.get("path")}
        if count > 1 and not replace_all:
            return {
                "ok": False,
                "error": f"old_text 出现了 {count} 次。请提供更精确的 old_text，或设置 replace_all=True。",
                "path": arguments.get("path"),
                "matches": count,
            }

        patched_text = original_text.replace(old_text, new_text) if replace_all else original_text.replace(old_text, new_text, 1)
        replacements = count if replace_all else 1
        atomic_write_text(target, patched_text, encoding=encoding)
        verified_text = target.read_text(encoding=encoding)

        if verified_text != patched_text:
            return {"ok": False, "error": "补丁已写入，但校验失败。", "path": arguments.get("path")}

        diff = _make_unified_diff(original_text, verified_text, result_path)
        added_lines, removed_lines = _count_diff_lines(diff)
        diff, diff_truncated = _truncate_diff(diff)

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
            "previous_version": before,
        }


def _make_unified_diff(old_text: str, new_text: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _count_diff_lines(diff: str) -> tuple[int, int]:
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


def _truncate_diff(diff: str) -> tuple[str, bool]:
    if len(diff) <= MAX_DIFF_CHARS:
        return diff, False
    return diff[:MAX_DIFF_CHARS], True
