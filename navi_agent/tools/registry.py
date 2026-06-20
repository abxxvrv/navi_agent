from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ..storage.safe_file import atomic_write_text, file_lock, file_version


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
            self._remember_read_version(result)
            return result

        if name in {"write_file", "patch_file"}:
            return self._invoke_file_write(tool, arguments)

        return tool.function(**arguments)

    def _remember_read_version(self, result: Any) -> None:
        if not isinstance(result, dict) or not result.get("ok") or not result.get("path"):
            return
        try:
            path = Path(str(result["path"])).resolve()
            version = file_version(path).to_dict()
        except Exception:
            return
        result["version"] = version
        self._read_versions[str(path)] = version

    def _invoke_file_write(self, tool: ToolSpec, arguments: dict) -> Any:
        path_arg = arguments.get("path")
        if not path_arg:
            return tool.function(**arguments)

        target = Path(str(path_arg)).resolve()
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
                result = self._safe_write_file(tool, arguments, before)
            else:
                result = self._safe_patch_file(tool, arguments, before)

            if isinstance(result, dict) and result.get("ok"):
                after = file_version(target).to_dict()
                result["version"] = after
                self._read_versions[str(target)] = after
            return result

    def _safe_write_file(self, tool: ToolSpec, arguments: dict, before: dict[str, Any]) -> Any:
        mode = arguments.get("mode", "overwrite")
        encoding = arguments.get("encoding", "utf-8")
        if mode != "overwrite":
            return tool.function(**arguments)

        target = Path(str(arguments["path"])).resolve()
        if target.exists() and target.is_dir():
            return {"ok": False, "error": "目标路径是目录，不是文件。", "path": arguments["path"]}

        old_text = ""
        if target.exists():
            try:
                old_text = target.read_text(encoding=encoding)
            except UnicodeDecodeError:
                return {
                    "ok": False,
                    "error": "目标文件不是指定编码下的有效文本。",
                    "path": arguments["path"],
                    "encoding": encoding,
                }

        new_text = str(arguments.get("content", ""))
        atomic_write_text(target, new_text, encoding=encoding)
        result = tool.function(path=str(arguments["path"]), content=new_text, mode="overwrite", encoding=encoding)
        if isinstance(result, dict):
            result["previous_version"] = before
        return result

    def _safe_patch_file(self, tool: ToolSpec, arguments: dict, before: dict[str, Any]) -> Any:
        target = Path(str(arguments["path"])).resolve()
        encoding = arguments.get("encoding", "utf-8")
        old_text = str(arguments.get("old_text", ""))
        new_text = str(arguments.get("new_text", ""))
        replace_all = bool(arguments.get("replace_all", False))

        if not target.exists():
            return {"ok": False, "error": "路径不存在。", "path": arguments["path"]}
        if not target.is_file():
            return {"ok": False, "error": "路径不是文件。", "path": arguments["path"]}
        if old_text == "":
            return {"ok": False, "error": "old_text 不能为空。", "path": arguments["path"]}

        try:
            original_text = target.read_text(encoding=encoding)
        except UnicodeDecodeError:
            return {
                "ok": False,
                "error": "文件不是指定编码下的有效文本。",
                "path": arguments["path"],
                "encoding": encoding,
            }

        count = original_text.count(old_text)
        if count == 0:
            return {"ok": False, "error": "文件中未找到 old_text。", "path": arguments["path"]}
        if count > 1 and not replace_all:
            return {
                "ok": False,
                "error": f"old_text 出现了 {count} 次。请提供更精确的 old_text，或设置 replace_all=True。",
                "path": arguments["path"],
                "matches": count,
            }

        patched_text = original_text.replace(old_text, new_text) if replace_all else original_text.replace(old_text, new_text, 1)
        atomic_write_text(target, patched_text, encoding=encoding)
        result = tool.function(**arguments)
        if isinstance(result, dict):
            result["previous_version"] = before
        return result
