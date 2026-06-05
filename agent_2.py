import os
import json
import operator
from datetime import datetime
from typing import Annotated, Literal
from typing_extensions import TypedDict

from dotenv import load_dotenv
from openai import OpenAI
from langgraph.graph import StateGraph, START, END

from context_manager import ContextManager
from history_utils import get_final_assistant_message
from session_store import SessionStore
from tool_registry import ToolRegistry, ToolSpec

from tool import (
    ListDirTool,
    ReadFileTool,
    WriteFileTool,
    PatchTool,
    LoadSkillTool,
    RunCommandTool,
)

load_dotenv()


WORKSPACE = "E:/light_agent"


# 0. Runtime context manager
context_manager = ContextManager(workspace=WORKSPACE)
session_store = SessionStore(root=f"{WORKSPACE}/.light_agent/sessions")


# 1. DeepSeek 原生 tools schema
tool_registry = ToolRegistry()

# 2. 真正执行的 Python 工具函数
def get_date_mock():
    return datetime.now().strftime("%Y-%m-%d")


def get_weather_mock(location: str, date: str):
    return f"{location} 在 {date} 的天气：多云，7~13°C"

tool_registry.register(
    name="get_date",
    description="获取当前日期。",
    parameters={
        "type": "object",
        "properties": {},
    },
    function=get_date_mock,
)

tool_registry.register(
    name="get_weather",
    description="获取指定地点在指定日期的天气。用户需要提供地点和日期。",
    parameters={
        "type": "object",
        "properties": {
            "location": {
                "type": "string",
                "description": "城市名称。",
            },
            "date": {
                "type": "string",
                "description": "日期，格式为 YYYY-mm-dd。",
            },
        },
        "required": ["location", "date"],
    },
    function=get_weather_mock,
)

list_dir_tool = ListDirTool(workspace=WORKSPACE)

tool_registry.register(
    name="list_dir",
    description="列出工作区内指定路径下的文件和目录。",
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的目录路径。使用 '.' 表示工作区根目录。",
                "default": ".",
            },
            "show_hidden": {
                "type": "boolean",
                "description": "是否包含隐藏文件和目录。",
                "default": False,
            },
            "max_items": {
                "type": "integer",
                "description": "最多返回的项目数量。",
                "default": 100,
                "minimum": 1,
                "maximum": 500,
            },
        },
        "required": [],
    },
    function=list_dir_tool,
)

read_file = ReadFileTool(workspace=WORKSPACE)

tool_registry.register(
    name="read_file",
    description=(
        "读取工作区内的 UTF-8 文本文件。"
        "当用户要求查看、解释、总结或调试文件时使用。"
        "路径必须是相对于工作区的路径。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "相对于工作区的文件路径，例如 'agent_2.py' 或 'src/main.py'。",
            },
            "start_line": {
                "type": "integer",
                "description": "读取的起始行号，从 1 开始。",
                "default": 1,
                "minimum": 1,
            },
            "max_lines": {
                "type": "integer",
                "description": "最多读取的行数。",
                "default": 200,
                "minimum": 1,
                "maximum": 500,
            },
            "max_chars": {
                "type": "integer",
                "description": "返回内容的最大字符数，默认 30000，范围 1000-50000。",
                "default": 30000,
                "minimum": 1000,
                "maximum": 50000,
            },
        },
        "required": ["path"],
    },
    function=read_file,
)

write_file = WriteFileTool(workspace=WORKSPACE)

tool_registry.register(
    name="write_file",
    description=(
    "向工作区内的文本文件写入内容。"
    "这个工具主要用于创建新文件、追加内容，或者在明确需要时替换整个文件。"
    "如果只是修改已有文件中的一小段代码，不要优先使用这个工具，应该优先使用 patch_file。"
    "只能写入工作区内的相对路径，不能使用绝对路径。"
    "如果父目录不存在，会自动创建父目录。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "要写入的文件相对路径，例如 "
                    "'main.py'、'README.md' 或 'tools/write_file.py'。"
                    "不允许使用绝对路径。"
                ),
            },
            "content": {
                "type": "string",
                "description": "要写入文件的文本内容。",
            },
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": (
                    "写入模式。'overwrite' 表示覆盖整个文件，'append' 表示追加到文件末尾。"
                    "修改已有文件的一小段内容时，不要使用 overwrite，应该使用 patch_file。"
                ),
            },
            "encoding": {
                "type": "string",
                "description": "文本编码，通常使用 'utf-8'。",
            },
        },
        "required": ["path", "content"],
    },
    function=write_file,
)

patch_file = PatchTool(workspace=WORKSPACE)

tool_registry.register(
    name="patch_file",
    description=(
        "对工作区内已有文本文件进行局部修改。"
        "这个工具通过查找精确匹配的 old_text，并将其替换为 new_text 来修改文件。"
        "当用户要求修改已有文件中的一小段代码、函数、配置或文本时，应优先使用这个工具，而不是 write_file。"
        "使用前通常应该先用 read_file 查看文件内容，确保 old_text 与文件中的内容完全一致。"
        "如果 old_text 在文件中出现多次，默认不会修改，除非明确设置 replace_all 为 true。"
        "只能修改工作区内的相对路径文件，不能使用绝对路径。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "相对于工作区的文件路径，例如 "
                    "'agent_2.py'、'tool.py' 或 'src/main.py'。"
                ),
            },
            "old_text": {
                "type": "string",
                "description": "要在文件中查找的精确文本。",
            },
            "new_text": {
                "type": "string",
                "description": "用于替换 old_text 的新文本。",
            },
            "replace_all": {
                "type": "boolean",
                "description": (
                    "是否替换所有匹配项。"
                    "为了更安全地修改代码，通常保持为 false。"
                ),
                "default": False,
            },
            "encoding": {
                "type": "string",
                "description": "文本编码，通常使用 'utf-8'。",
                "default": "utf-8",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
    function=patch_file,
)

load_skill = LoadSkillTool(workspace=WORKSPACE)

tool_registry.register(
    name="load_skill",
    description=(
        "按名称加载一个技能，使其在下一次模型调用时进入系统提示词。"
        "当技能索引显示某个技能相关，但完整 SKILL.md 指令尚未加载时使用。"
        "一次只能加载一个技能。"
        "如果当前任务需要加载技能，请优先单独调用本工具；不要在同一轮同时调用其他工具。"
        "技能会在下一次模型调用时生效。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能索引中的技能文件夹名称，例如 'frontend-design' 或 'docx'。",
            },
        },
        "required": ["name"],
    },
    function=load_skill,
)

run_command = RunCommandTool(workspace=WORKSPACE)

tool_registry.register(
    name="run_command",
    description=(
        "在工作区内运行一个短时间、非交互式的终端命令。"
        "主要用于验证代码、运行测试、检查语法或查看错误信息。"
        "命令会在固定工作区内执行，并带有超时限制。"
        "不要用于长期运行服务、安装依赖、访问网络或执行危险系统操作。"
        "修改代码后，如果需要验证结果，可以使用这个工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": (
                    "要运行的终端命令。应该是短时间、非交互式命令，"
                    "例如 'python demo.py'、'python -m py_compile demo.py'、"
                    "'pytest' 或 'node --check main.js'。"
                ),
            },
            "cwd": {
                "type": "string",
                "description": (
                    "命令运行目录，必须是相对于工作区的路径。"
                    "通常使用 '.' 表示工作区根目录。"
                ),
                "default": ".",
            },
            "timeout_seconds": {
                "type": "integer",
                "description": (
                    "命令超时时间，单位秒。默认 10 秒，最大 30 秒。"
                ),
                "default": 10,
                "minimum": 1,
                "maximum": 30,
            },
            "encoding": {
                "type": "string",
                "description": "输出解码使用的编码，通常使用 'utf-8'。",
                "default": "utf-8",
            },
        },
        "required": ["command"],
    },
    function=run_command,
)

# 3. DeepSeek client
client = OpenAI(
    api_key=os.environ["DEEPSEEK_API_KEY"],
    base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
)


# 4. LangGraph 状态
class AgentState(TypedDict):
    messages: Annotated[list[dict], operator.add]
    active_skills: list[str]


# 5. 把 DeepSeek 返回的 message 转成 dict
def assistant_message_to_dict(message):
    data = {
        "role": "assistant",
        "content": message.content or "",
    }

    reasoning_content = getattr(message, "reasoning_content", None)
    if reasoning_content is not None:
        data["reasoning_content"] = reasoning_content

    if message.tool_calls:
        data["tool_calls"] = [
            tool_call.model_dump(exclude_none=True)
            for tool_call in message.tool_calls
        ]

    return data


# 6. LLM 节点：调用 DeepSeek 原生 API
def llm_node(state: AgentState):
    skill_index_prompt = context_manager.build_skill_index_prompt()

    runtime_messages = context_manager.build_runtime_messages(
        messages=state["messages"],
        active_skills=state.get("active_skills", []),
        extra_instructions=skill_index_prompt,
    )

    response = client.chat.completions.create(
        model="deepseek-v4-flash",
        messages=runtime_messages,
        tools=tool_registry.to_openai_tools(),
        reasoning_effort="high",
        extra_body={"thinking": {"type": "enabled"}},
    )

    message = response.choices[0].message
    assistant_message = assistant_message_to_dict(message)

    print("\n[模型内容]")
    print(assistant_message.get("content"))

    print("\n[模型工具调用]")
    print(assistant_message.get("tool_calls"))

    return {
        "messages": [assistant_message]
    }


# 7. 工具节点：执行模型请求的工具
def tool_node(state: AgentState):
    last_message = state["messages"][-1]
    tool_messages = []
    active_skills = list(state.get("active_skills", []))

    for tool_call in last_message.get("tool_calls", []):
        tool_name = tool_call["function"]["name"]
        tool_args = json.loads(tool_call["function"]["arguments"] or "{}")

        tool_result = tool_registry.invoke(tool_name, tool_args)

        if tool_name == "load_skill" and tool_result.get("ok"):
            skill_name = tool_result["skill_name"]
            if skill_name not in active_skills:
                active_skills.append(skill_name)

        print(f"\n[{tool_name} 工具结果]")
        print(tool_result)

        tool_messages.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],
            "content": json.dumps(tool_result, ensure_ascii=False),
        })

    return {
        "messages": tool_messages,
        "active_skills": active_skills,
    }


# 8. 条件边：判断下一步去 tool_node 还是结束
def should_continue(state: AgentState) -> Literal["tool_node", "__end__"]:
    last_message = state["messages"][-1]

    if last_message.get("tool_calls"):
        return "tool_node"

    return END

# 多轮对话的函数
def run_chat():
    semantic_history = []

    print("Light Agent 已启动。输入 'exit' 退出。")

    while True:
        try:
            user_input = input("\n用户: ")
        except EOFError:
            print("输入结束，退出。")
            break

        user_input = user_input.lstrip("\ufeff").strip()

        if not user_input:
            continue

        if user_input.lower() in ["exit", "quit", "q"]:
            print("再见。")
            break

        user_message = {
            "role": "user",
            "content": user_input,
        }

        turn_state: AgentState = {
            "messages": semantic_history + [user_message],
            "active_skills": [],
        }

        result = agent.invoke(turn_state)
        current_turn_messages = result["messages"][len(semantic_history):]
        final_message = get_final_assistant_message(current_turn_messages)

        if final_message is None:
            final_message = {
                "role": "assistant",
                "content": "",
            }

            print("\nAssistant:")
            print("本轮没有得到有效最终回复。")
            continue

        print("\nAssistant:")
        print(final_message["content"])

        semantic_history.append(user_message)
        semantic_history.append({
            "role": "assistant",
            "content": final_message["content"],
        })


# 9. 构建 LangGraph
graph_builder = StateGraph(AgentState)

graph_builder.add_node("llm_node", llm_node)
graph_builder.add_node("tool_node", tool_node)

graph_builder.add_edge(START, "llm_node")

graph_builder.add_conditional_edges(
    "llm_node",
    should_continue,
    {
        "tool_node": "tool_node",
        END: END,
    },
)

graph_builder.add_edge("tool_node", "llm_node")

agent = graph_builder.compile()


# 10. 运行
# result = agent.invoke({
#     "messages": [
#         {
#             "role": "user",
#             "content": "当前文件夹下有哪些文件？"
#         }
#     ]
# })

# print("\n[Final Answer]")
# print(result["messages"][-1]["content"])

run_chat()
