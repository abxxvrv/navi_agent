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
        return tool.function(**arguments)