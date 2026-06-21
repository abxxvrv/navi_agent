from dataclasses import dataclass
from typing import Any, Callable


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

        return self._tools[name].function(**arguments)
