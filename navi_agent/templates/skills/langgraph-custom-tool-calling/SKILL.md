---
name: langgraph-custom-tool-calling
description: 在LangGraph中使用ToolNode时保留自定义工具调用构造方式。当模型不支持标准bind_tools()、需要手动解析流式响应中的tool_calls、或要从手写工具执行逻辑迁移到ToolNode时使用。
---

# LangGraph 自定义工具调用集成

## 核心约束

ToolNode 的 `invoke()` 内部用 `isinstance(m, AIMessage)` 查找最新 assistant 消息。**plain dict 不会被识别**，必须返回 `AIMessage` 实例。

## 完整模式：自定义 LLM + ToolNode

### 1. llm_node 返回 AIMessage（不是 dict！）

```python
import json
from langchain_core.messages import AIMessage

def _llm_node(state):
    messages = state["messages"]

    # history 可能含 AIMessage/ToolMessage，转为 dict 给 OpenAI SDK
    api_messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in messages:
        if isinstance(m, dict):
            api_messages.append(m)
        else:
            d = {"role": getattr(m, "role", "assistant"),
                 "content": getattr(m, "content", "") or ""}
            tc = getattr(m, "tool_calls", None)
            if tc:
                # ⚠️ AIMessage 存的是 LangChain 格式，必须转回 OpenAI 格式
                d["tool_calls"] = [
                    {"id": t["id"], "type": "function",
                     "function": {"name": t["name"],
                                  "arguments": json.dumps(t.get("args", {}), ensure_ascii=False)}}
                    for t in tc
                ]
            tcid = getattr(m, "tool_call_id", None)
            if tcid:
                d["tool_call_id"] = tcid
            rkc = getattr(m, "additional_kwargs", {}).get("reasoning_content")
            if rkc:
                d["reasoning_content"] = rkc
            api_messages.append(d)

    # --- 自定义流式调用（保持你的 reasoning_content、stream 逻辑） ---
    stream = your_llm_stream(api_messages, tools_schema)
    content, tool_calls_raw, reasoning = parse_stream(stream)  # 你的解析逻辑

    # --- OpenAI 格式 → LangChain 格式 ---
    lc_tool_calls = []
    for tc in tool_calls_raw:
        args_str = tc.get("function", {}).get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except Exception:
            args = {"raw": args_str}
        lc_tool_calls.append({
            "name": tc["function"]["name"],
            "args": args,
            "id": tc.get("id", ""),
            "type": "tool_call",
        })

    ai_msg = AIMessage(content=content, tool_calls=lc_tool_calls)
    if reasoning:
        ai_msg.additional_kwargs["reasoning_content"] = reasoning

    return {"messages": [*messages, ai_msg]}
```

### 2. 条件边兼容两种消息类型

```python
from langchain_core.messages import AIMessage

def _should_continue(state):
    last = state["messages"][-1]
    if isinstance(last, AIMessage):
        return "tools" if last.tool_calls else END
    return "tools" if last.get("tool_calls") else END
```

### 3. 兼容辅助函数（dict ↔ LangChain 混合 history）

history 会同时包含 plain dict（user/tool 消息）和 AIMessage/ToolMessage，需要用兼容函数访问：

```python
def _msg_attr(m, key, default=None):
    if isinstance(m, dict):
        return m.get(key, default)
    # 先试直接属性，再试 additional_kwargs（reasoning_content 存在这里）
    val = getattr(m, key, None)
    if val is not None:
        return val
    return getattr(m, "additional_kwargs", {}).get(key, default)
```

## 格式对照

| 字段 | OpenAI API 格式 | LangChain AIMessage 格式 |
|------|-----------------|-------------------------|
| tool_calls | `[{"id":"x", "type":"function", "function":{"name":"...", "arguments":"..."}}]` | `[{"name":"...", "args":{...}, "id":"x", "type":"tool_call"}]` |
| tool_call_id | `"tool_call_id": "x"` | `ToolMessage.tool_call_id` 属性 |
| reasoning | `"reasoning_content": "..."` (顶层) | `additional_kwargs["reasoning_content"]` |

## ⚠️ 双向转换是关键

history 转 dict 发给 API 时，**必须**把 LangChain 格式的 tool_calls 转回 OpenAI 格式：
- `t["name"]` → `function.name`
- `json.dumps(t["args"])` → `function.arguments`

直接 `d["tool_calls"] = tc` 会把 LangChain 格式发给 API，模型无法识别。

## 常见错误

| 错误 | 原因 | 修复 |
|------|------|------|
| `ValueError: No AIMessage found in input` | _llm_node 返回 plain dict | 返回 AIMessage 实例 |
| `tool_call() got an unexpected keyword argument 'function'` | 传了 OpenAI 格式 tool_calls 给 AIMessage | 转为 LangChain 格式 |
| `AttributeError: 'AIMessage' object has no attribute 'get'` | 用 `.get()` 访问 AIMessage | 用 `getattr` 或 `_msg_attr` |
| API 返回格式错误或工具调用失败 | history 转 dict 时没把 LangChain tool_calls 转回 OpenAI 格式 | 双向转换 |

## 适用场景

- 模型不支持标准 bind_tools() 接口
- 需要处理流式响应中的 tool_calls
- 需要保留自定义的 reasoning_content 处理
- 从手写工具执行逻辑迁移到 LangGraph 标准组件
- 需要注入外部依赖（数据库连接等）到工具函数
