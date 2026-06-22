---
name: navi-tool-dev
description: Navi Agent 工具开发指南。当需要为 Navi 添加新工具（tool）、修改已有工具、或理解工具注册机制时使用。触发场景：用户说"写一个 XX 工具"、"添加工具"、"tool 怎么写"、"注册工具"、涉及 tool.py 或 runtime.py 中工具注册部分的修改。也适用于：集成外部工具协议（MCP 等）、添加工具子系统、批量注册动态发现的工具。也适用于：添加或修改斜杠命令（/xxx）、斜杠命令子命令。
---

# Navi 工具开发指南

## 架构概览

工具系统由三个文件组成：

| 文件 | 职责 |
|------|------|
| `navi_agent/tool_registry.py` | `ToolRegistry` 类：存储工具、转 OpenAI tools schema、按名调用 |
| `navi_agent/tool.py` | 所有工具类的实现 |
| `navi_agent/runtime.py` | 导入工具类 → `registry.register()` 注册 |
| `navi_agent/approval.py` | 工具审批分类（READ_ONLY / WRITE / COMMAND） |

## 创建新工具：四步（第三步容易遗漏！）

### Step 1：在 `tool.py` 中实现工具类

```python
class MyTool:
    """工具简述。"""

    def __init__(self, workspace: Path, config_path: Path = None, session_meta: dict = None):
        self.workspace = workspace
        # 从 config.json 读取凭证（推荐，与 ModelRouter 一致）
        if config_path:
            self.api_url, self.api_key = self._load_credentials(config_path)
        # 接收 session meta 引用（需要感知运行时状态时）
        self.session_meta = session_meta

    @staticmethod
    def _load_credentials(config_path: Path) -> tuple[str, str]:
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            return "", ""
        provider = cfg.get("providers", {}).get("provider_name", {})
        base_url = provider.get("base_url", "").rstrip("/")
        api_key = provider.get("api_key", "")
        return f"{base_url}/endpoint", api_key

    def __call__(self, param1: str, param2: int = 10) -> dict[str, Any]:
        if not param1.strip():
            return {"ok": False, "error": "param1 不能为空。"}
        try:
            result = do_something(param1, param2)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "result": result}
```

**关键约定：**
- 返回值统一为 `dict[str, Any]`，必须含 `"ok": True/False`
- 错误时返回 `{"ok": False, "error": "描述"}`
- 成功时返回 `{"ok": True, ...业务字段}`
- 路径解析用 `resolve_path(workspace, path)` 或手动处理绝对/相对路径

**⚠️ 工具输出架构**：工具可能有两层输出通道——实时流（`on_output`）和结果渲染（`event_handler`）。确保两层不输出相同内容，否则用户看到双份输出。详见 `cli-tool-output-arch` 技能。

### Step 2：在 `runtime.py` 中注册

**a) 添加 import（文件头部）：**

```python
from .tool import (
    ...
    MyTool,        # ← 新增
)
```

**b) 注册（在 `_register_tools` 方法末尾）：**

```python
self.tool_registry.register(
    name="my_tool",
    description="""
- 工具的用途说明，面向 LLM 阅读。
- 每条说明以 "- " 开头。
- 说明触发条件和返回值含义。
- 如果其他工具应引导用户使用本工具，在描述中注明交叉引用。
""",
    parameters={
        "type": "object",
        "properties": {
            "param1": {
                "type": "string",
                "description": "参数说明。",
            },
            "param2": {
                "type": "integer",
                "description": "可选参数说明。",
                "default": 10,
            },
        },
        "required": ["param1"],
    },
    function=MyTool(
        workspace=self.workspace,
        config_path=get_config_path(),       # 需要凭证时
        session_meta=self.session_store.meta, # 需要感知运行时状态时
    ),
)
```

### Step 3：在 `approval.py` 中注册审批类别 ⚠️

**这一步容易遗漏！** 不注册的话，strict 模式下工具调用会被拒绝。

```python
# approval.py 中找到对应集合，添加工具名
READ_ONLY_TOOLS = {
    # ... 已有工具 ...
    "my_tool",  # ← 新增
}
```

| 集合 | 用途 | 判断依据 |
|------|------|---------|
| `READ_ONLY_TOOLS` | 只读，自动允许 | 不修改文件系统、不执行命令 |
| `WRITE_TOOLS` | 写入，按规则审批 | 修改文件（write_file, patch_file 等） |
| `COMMAND_TOOLS` | 命令执行，最严格 | 执行 shell 命令（run_command） |

### Step 4：验证

```bash
.venv/Scripts/python.exe -c "from navi_agent.runtime import AgentRuntime; print('OK')"
```

---

## 参数设计原则

**避免"魔法默认值 + 条件拼接"模式**。

❌ 旧模式（复杂）：
```python
# 工具 schema 中
"default": "请描述这张图片的内容"

# 辅助方法中
if prompt and prompt.strip() != "请描述这张图片的内容":
    full_prompt = "英文前缀:\n\n" + prompt
else:
    full_prompt = "英文长句"
```

✅ 新模式（简洁）：
```python
# 工具 schema 中 —— 默认值就是你想要的
"default": "Fully describe and explain everything about this image"

# 辅助方法中 —— 直接透传
full_prompt = prompt or "fallback"
```

**原则**：
1. Schema 中的 `default` 直接设为下游系统需要的值，不要设一个"人看的默认值"再在代码里替换
2. 实现层只做 `prompt or fallback` 的空值兜底，不做"检测默认值 → 替换/拼接"的条件逻辑
3. 这样用户传什么就直接用什么，没有隐藏行为

---

## 斜杠命令开发

### 文件位置

| 文件 | 职责 |
|------|------|
| `navi_agent/cli/main.py` | `SLASH_COMMANDS` 列表 + `handle_slash_command()` 处理函数 |
| `navi_agent/cli/chat_controller.py` | `process_message()` 中的 `/model` 交互式处理 |

### 添加顶级斜杠命令

**Step 1：** 在 `SLASH_COMMANDS` 列表中添加命令名（`main.py` 顶部）

```python
SLASH_COMMANDS = [
    "/help",
    "/clear",
    "/tools",
    # ...
    "/my_command",  # ← 新增
]
```

**Step 2：** 在 `handle_slash_command()` 中添加处理逻辑

```python
if command == "/my_command":
    # 处理逻辑
    console.print("[bold]My command output[/bold]")
    return True  # True = 已处理，不交给模型
```

**Step 3：** 在 `print_chat_help()` 中添加帮助条目

### 添加子命令模式（推荐）

当命令需要子命令（如 `/mcp status`、`/mcp add`）时：

**Step 1：** 在 `SLASH_COMMANDS` 列表中添加所有子命令

```python
SLASH_COMMANDS = [
    "/help",
    "/mcp",
    "/mcp status",    # ← 子命令
    "/mcp add",       # ← 子命令
    "/mcp remove",    # ← 子命令
    "/mcp reload",    # ← 子命令
    "/mcp help",      # ← 子命令
]
```

**Step 2：** 用 `command.startswith()` 匹配，从命令字符串解析参数

```python
if command.startswith("/mcp"):
    try:
        from ..integrations.mcp_commands import handle_mcp_command
        # 从命令中提取参数："/mcp status" → "status"
        mcp_args = command[4:].strip() if len(command) > 4 else ""
        result = handle_mcp_command(mcp_args, runtime.tool_registry)
        console.print(result)
    except ImportError:
        console.print("[yellow]MCP module not available.[/yellow]")
    except Exception as e:
        console.print(f"[red]MCP command error: {e}[/red]")
    return True
```

**关键点：**
- 用 `command.startswith("/xxx")` 而非 `command == "/xxx"`
- 从命令字符串切片提取参数：`command[len("/xxx"):].strip()`
- 不带参数时默认执行某个子命令（如 `status`）
- 帮助信息中注明支持的子命令：`Manage MCP servers (status/add/remove/reload/help)`

### 补全与帮助

- 输入框补全通过 `WordCompleter(SLASH_COMMANDS)` 实现，子命令会自动出现在补全列表中
- `/help` 的帮助文本在 `print_chat_help()` 函数中定义

---

## 工具子系统集成（MCP 等协议）

当需要集成外部工具协议（如 MCP）时，采用"后台 event loop + 工具桥接"模式：

### 架构

```
config.json (mcp_servers)
       ↓
   MCPManager (后台 daemon thread + asyncio loop)
       ↓
   MCPServerTask × N (每个 server 一个长连接)
       ↓
   ToolRegistry.register() (动态注册 mcp_{server}_{tool})
```

### 核心模块划分

| 文件 | 职责 |
|------|------|
| `mcp_client.py` | 连接管理、工具发现、调用桥接、重连、熔断 |
| `mcp_commands.py` | CLI 命令（/mcp add/remove/reload/status） |

### 关键设计点

**1. 后台 Event Loop**

```python
_mcp_loop: Optional[asyncio.AbstractEventLoop] = None
_mcp_thread: Optional[threading.Thread] = None

def _ensure_mcp_loop():
    """启动后台 asyncio event loop（daemon thread）"""
    global _mcp_loop, _mcp_thread
    with _lock:
        if _mcp_loop is not None and _mcp_loop.is_running():
            return
        _mcp_loop = asyncio.new_event_loop()
        def _run_loop():
            asyncio.set_event_loop(_mcp_loop)
            _mcp_loop.run_forever()
        _mcp_thread = threading.Thread(target=_run_loop, daemon=True)
        _mcp_thread.start()

def _run_on_mcp_loop(coro, timeout: float = 120) -> Any:
    """在后台 loop 上运行协程（阻塞当前线程）"""
    future = asyncio.run_coroutine_threadsafe(coro, _mcp_loop)
    return future.result(timeout=timeout)
```

**2. 同步 Handler 桥接异步调用**

```python
def _make_tool_handler(server_name: str, tool_name: str, timeout: float):
    """返回同步 handler，内部调用异步 MCP 工具"""
    def _handler(**kwargs) -> str:
        async def _call():
            result = await server.session.call_tool(tool_name, arguments=kwargs)
            # 处理结果...
            return json.dumps({"result": text}, ensure_ascii=False)
        
        # ⚠️ 注意：必须调用 _call() 得到协程对象，不是传 _call 函数
        return _run_on_mcp_loop(_call(), timeout=timeout)
    
    return _handler
```

**3. 工具命名约定**

外部工具注册时加前缀避免冲突：`mcp_{server_name}_{tool_name}`

**4. 集成到 runtime.py**

```python
# _register_tools() 之后调用
def _init_mcp_tools(self) -> None:
    try:
        from .mcp_client import discover_mcp_tools, _MCP_AVAILABLE
        if _MCP_AVAILABLE:
            mcp_tools = discover_mcp_tools(self.tool_registry)
    except Exception as e:
        logger.debug("MCP initialization failed (non-fatal): %s", e)
```

### MCP 工具生命周期与 `_tools_for_api`

Navi 有两套工具集合，理解它们的关系是 MCP 集成的关键：

| 集合 | 用途 | 更新时机 |
|------|------|---------|
| `tool_registry` | 运行时工具注册表，存储实际可执行的 handler | MCP 连接/断开时动态增删 |
| `_tools_for_api` | 发给模型的工具列表（OpenAI format） | `__init__` 时构建一次，会话内固定 |

**`_tools_for_api` 构建流程（agent.py 第 190-207 行）：**

```python
self._init_mcp_tools()  # 连接 MCP，工具注册到 tool_registry

all_tools_for_api = self.tool_registry.to_openai_tools()  # 取所有已注册工具
persisted_tool_names = self.session_store.meta.get("tool_names") or []

if resume_session_id and persisted_tool_names:
    # Resume：只保留上次会话记录的工具名
    allowed = set(persisted_tool_names)
    self._tools_for_api = [t for t in all_tools_for_api
                           if t.get("function", {}).get("name") in allowed]
else:
    # 新会话：全部工具，记录到 session meta 供 resume 用
    self._tools_for_api = all_tools_for_api
    self.session_store.set_tool_names([...])
```

**会话内每轮请求都传同一个 list 引用：**

```python
# agent.py 第 601 行
stream = run_model_stream(..., tools=self._tools_for_api)
```

**Resume 时的工具列表行为：**

- 即使 MCP 连接成功、新工具注册了，resume 时 `_tools_for_api` 只包含 `persisted_tool_names` 中的工具
- 新 MCP 工具对模型不可见，必须开新会话
- 这是有意设计：保持 resume 会话的工具集与上次一致

**`/mcp reload` 的限制：**

- `reload_mcp_servers(registry)` 只操作 `tool_registry`，不更新 `_tools_for_api`
- reload 后新工具对当前会话的模型仍然不可见

### MCP 工具调用错误处理

**调用链：** model → `_execute_single_tool()` → `tool_registry.invoke()` → MCP handler

**工具未注册时（registry.py 第 78 行）：**

```python
def invoke(self, name: str, arguments: dict) -> Any:
    if name not in self._tools:
        raise ValueError(f"Unknown tool: {name}")
```

`_execute_single_tool` 捕获异常后返回给模型：`{"ok": false, "error": "Unknown tool: xxx"}`

**MCP server 未连接时（mcp_client.py 第 455-461 行）：**

```python
with _lock:
    server = _servers.get(server_name)
if not server or not server.session:
    _bump_server_error(server_name)
    return json.dumps({
        "error": f"MCP server '{server_name}' is not connected"
    }, ensure_ascii=False)
```

**熔断器（Circuit Breaker）：**

连续失败 N 次后，短路后续调用，在冷却期内直接返回错误，不再尝试连接：

```python
if _server_error_counts.get(server_name, 0) >= _CIRCUIT_BREAKER_THRESHOLD:
    age = time.monotonic() - _server_breaker_opened_at.get(server_name, 0.0)
    if age < _CIRCUIT_BREAKER_COOLDOWN_SEC:
        return json.dumps({"error": f"MCP server '{server_name}' is unreachable..."})
```

### MCP 工具列表管理设计讨论

**当前设计的问题：**

1. Resume 后新 MCP 工具对模型不可见
2. `/mcp reload` 后新工具对当前会话不可见
3. 必须开新会话才能使用新工具

**设计思路对比：**

| 方案 | 好处 | 坏处 |
|------|------|------|
| 工具列表基于配置（不管连接状态） | MCP 重连后立即可用 | 不可用时浪费 token、误导模型 |
| 工具列表基于连接状态（当前设计） | 不浪费 token、不误导 | MCP 重连后不可用，必须开新会话 |

**Claude Code 的做法：**

```typescript
// query.ts 第 1659-1668 行
// 每轮请求前刷新工具列表
if (updatedToolUseContext.options.refreshTools) {
  const refreshedTools = updatedToolUseContext.options.refreshTools()
  if (refreshedTools !== updatedToolUseContext.options.tools) {
    updatedToolUseContext = {
      ...updatedToolUseContext,
      options: {
        ...updatedToolUseContext.options,
        tools: refreshedTools,
      },
    }
  }
}
```

**Claude Code 的两种机制：**

1. **每轮请求前刷新工具列表**：直接更新 API 请求的 tools 参数
2. **用 `<system-reminder>` 标签通知工具变化**：在 messages 中插入系统提醒

**Prompt caching 的影响：**

- prompt caching 基于前缀匹配
- 如果 tools 参数变化了，缓存会失效
- Claude Code 用引用比较（`!==`）避免不必要的缓存失效

**改进方案（待实现）：**

1. Resume 时合并 `persisted_tool_names` + 当前已注册工具（不排除新工具）
2. `/mcp reload` 后更新 `_tools_for_api`
3. MCP 工具未注册时返回友好错误信息

### 与 Hermes 的对比

Hermes 的 MCP 工具处理方式与 Navi 类似，但有一些差异：

| 方面 | Navi | Hermes |
|------|------|--------|
| 工具注册 | `_register_server_tools()` | `_register_server_tools()` |
| 连接失败处理 | 跳过，不注册工具 | 同样跳过 |
| 工具名前缀 | `mcp_{server}_{tool}` | `mcp_{server}_{tool}` |
| 后台 loop | 单 daemon thread | 单 daemon thread |
| OAuth 支持 | 无 | 完整（mcp_oauth.py + mcp_oauth_manager.py） |
| 动态工具刷新 | 无 | 有（`tools/list_changed` notification） |
| 并行调用标记 | 无 | `supports_parallel_tool_calls` 配置 |

## ⚠️ 异步调用常见 Bug

**协程函数 vs 协程对象：**

```python
# ❌ 错误：传入协程函数，不是协程对象
result = _run_on_mcp_loop(_call, timeout=30)
# 报错：A coroutine object is required

# ✅ 正确：调用函数得到协程对象
result = _run_on_mcp_loop(_call(), timeout=30)
```

**原因：** `async def _call()` 定义的是协程函数，必须 `_call()` 调用后才得到协程对象。`asyncio.run_coroutine_threadsafe()` 需要的是协程对象。

## 凭证管理

| 模式 | 用于 | 读取方式 |
|------|------|---------|
| `config.json` providers | 需要多 provider 路由的 API（如 MiMo、DeepSeek） | 从 `config.json` 的 `providers.<name>.api_key` + `base_url` 读取 |
| 环境变量 | 单一外部服务（如 Tavily） | `os.environ.get("API_KEY")` |
| `config.json` mcp_servers | MCP 服务器配置 | 从 `config.json` 的 `mcp_servers.<name>` 读取 |

**推荐使用 config.json 模式**，与 ModelRouter 保持一致。

## 运行时感知：session_meta 模式

当工具需要感知当前会话状态（如当前模型、provider）时，接收 `session_store.meta` 的 dict 引用：

```python
def __init__(self, workspace: Path, session_meta: dict):
    self.session_meta = session_meta  # dict 引用，/model 切换后自动更新

def _check_current_model(self) -> str:
    """每次调用时实时读取，反映 /model 切换。"""
    provider = self.session_meta.get("provider", "")
    model = self.session_meta.get("model", "")
    if not provider or not model:
        # fallback 到 config.json
        ...
    return model
```

**不要在 `__init__` 时缓存运行时状态**（如 `self.current_model = ...`），否则 `/model` 等操作切换后不生效。

## 工具返回特殊控制信号

工具可以通过返回 dict 中的 `_` 前缀字段触发 agent loop 的特殊行为：

```python
# 多模态结果（vision_analyze 使用）
return {
    "ok": True,
    "content": "prompt text",
    "_multimodal": True,
    "_image_data_url": "data:image/jpeg;base64,...",
}
```

**约定**：下划线前缀的字段是 agent loop 的控制信号，不序列化到 tool message 中。当前支持：
- `_multimodal`: agent loop 将图片注入为 user message（因为大多数 API 不支持 tool message 中的图片）
- `_image_data_url`: 配合 `_multimodal` 使用的 base64 图片数据

## HTTP API 调用模式

项目使用标准库 `urllib.request`，不引入 `requests` 等第三方 HTTP 库：

```python
import urllib.request
import urllib.error

body = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    url,
    data=body,
    headers={"Content-Type": "application/json", "api-key": api_key},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    data = json.loads(resp.read().decode("utf-8"))
```

错误处理三件套：`urllib.error.HTTPError`、`urllib.error.URLError`、兜底 `Exception`。

## description 写作规范

工具的 `description` 是给 LLM 看的触发条件，直接影响工具是否被正确选择：

- 用 `- ` 开头的列表格式
- 第一条说明工具的核心用途
- 后续条目说明适用场景、限制、返回值含义
- 如果其他工具应引导到本工具，描述中注明（如 read_file 遇到图片提示用 vision_analyze）
- 不要写使用示例（LLM 不需要）
- 参考项目中已有工具的 description 风格
