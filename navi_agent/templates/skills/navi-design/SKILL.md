---
name: navi-design
description: Navi Agent 设计参考库。当用户询问 navi agent 的架构设计、功能方案、技术选型、或某个具体实现应该如何做时，自动参考 Claude Code (cc_src/)、KIMI-CLI (kimi-cli/)、Hermes (hermes-agent/) 三个成熟项目的源码来提供建议。触发场景包括：讨论 agent loop 设计、工具系统、审批机制、会话管理、UI/CLI 交互、技能系统、插件机制、subagent、MCP 集成等任何 navi 的功能设计问题。也适用于：添加新工具、修改工具行为、集成外部 API。也适用于：多智能体系统设计、Worker 模式、sub_agent 委派模式、Interactive SubAgent 模式、Human-in-the-Loop 模式。也适用于：LangGraph 功能评估、工具调用兼容性分析、模型适配方案选型。
---

# Navi Agent 设计参考技能

当用户询问 navi 的功能设计、架构方案时，参考三个成熟项目的实现来提供建议。

## 参考项目位置

| 项目 | 绝对路径 | 语言 |
|------|---------|------|
| Claude Code | `E:\light_agent\cc_src\src\` | TypeScript (React/Ink) |
| KIMI-CLI | `E:\light_agent\kimi-cli\src\kimi_cli\` | Python (asyncio + kosong) |
| Hermes | `E:\light_agent\hermes-agent\` | Python (同步 + 多线程) |
| Navi (当前) | `E:\light_agent\navi_agent\` | Python (LangGraph + MiMo) |
| MiMo-Code | `E:\light_agent\MiMo-Code\packages\opencode\src\` | TypeScript (Effect) |

## 使用方法

1. **识别设计领域**
2. **查阅 reference 文件**：`C:\Users\29924\.navi\skills\navi-design\references\{claude-code,kimi-cli,hermes}.md`
3. **对比三个项目的做法**
4. **给出 navi 方案**

## 设计领域速查

| 领域 | Claude Code | KIMI-CLI | Hermes |
|------|-------------|----------|--------|
| Agent Loop | LangGraph state graph | asyncio 手写循环 | 同步 while 循环 + thread pool |
| 工具注册 | 静态 import + feature flag | import path 字符串加载 | 自注册 registry + AST 扫描 |
| UI | Ink (React TUI) | Wire 协议 + Rich Live + Shell UI | Ink TUI + 多平台 gateway |
| 上下文压缩 | Microcompact + Compaction | SimpleCompaction | ContextCompressor |
| 图片/视觉 | 集成在 ReadFile 内（多模态直传） | — | 独立 vision_analyze + 辅助视觉管道 |
| MCP 集成 | ✅ 完整实现（见下方） | ❌ 未实现 | ✅ 完整实现（见下方） |
| 会话搜索 | ❌ 未实现 | ❌ 未实现 | ✅ FTS5 全文搜索（见下方） |
| 命令审批 | seccomp 拦截 | 无 | 正则模式 + 辅助 LLM + 分割检查 |
| 会话创建 | 启动时创建 | 启动时创建 | 启动时创建 |
| 输入框滚动 | viewport 字符偏移（见下方） | — | prompt_toolkit 内置滚动（见下方） |

## Navi 当前状态

- **Agent Loop**: LangGraph `StateGraph` (llm_node ↔ tool_node)
- **工具系统**: 类实例 callable，手动在 `runtime.py:_register_tools()` 注册
- **模型**: OpenAI-compatible API（默认 MiMo），通过 ModelRouter 路由
- **CLI**: 增强版 CLI — `terminal_ui.py`（Rich Live + PromptSession），不用全屏 TUI
- **MCP**: ✅ 已实现基础集成
- **会话搜索**: ✅ 已实现（FTS5 + 三模式）
- **命令审批**: 三级模式（strict/normal/open）+ session allowlist

## Navi 源码关键位置

| 文件 | 用途 |
|------|------|
| `runtime/agent.py` | AgentRuntime — llm_node + tool_node + graph + 流式事件 |
| `tools/builtin.py` | 所有工具类（ReadFileTool, WriteFileTool, PatchTool, RunCommandTool, SearchSessionTool 等)|
| `tools/registry.py` | ToolRegistry — 工具注册表（ToolSpec + register + invoke）|
| `storage/history_store.py` | HistoryStore — SQLite 会话存储 + FTS5 搜索 |
| `integrations/mcp_client.py` | MCP 客户端核心 |
| `integrations/mcp_commands.py` | /mcp 斜杠命令 |
| `model/router.py` | ModelRouter — 流式调用 + provider 切换 |
| `tools/approval.py` | 三级审批（strict/normal/open）|
| `cli/stream_box.py` | 流式输出框 |
| `paths.py` | get_navi_home、get_config_path |

---

# 命令审批系统设计

## 三方对比

| 特性 | Navi | Hermes | Claude Code |
|------|------|--------|-------------|
| 审批模式 | strict/normal/open | manual/smart/off | 无（seccomp 拦截） |
| 硬阻断 | 9 条正则 | 12 条正则 + sudo guard | seccomp 系统调用拦截 |
| 危险模式 | 5 类正则 | 47 条正则 | 无（容器隔离） |
| 会话放行 | approval_key 精确匹配 | pattern_key 描述匹配 | 无 |
| 智能审批 | 无 | 辅助 LLM 评估风险 | 无 |
| 分割检查 | 无 | 无 | 无 |

## Navi approval_key 生成逻辑

```python
# tools/approval.py make_command_approval_key()

# 预定义 scope（前缀匹配）→ 批准一个放行同类
scopes = [
    (("pip", "install"),       "shell:scope:pip install"),
    (("npm", "install"),       "shell:scope:npm install"),
    (("git", "add"),           "shell:scope:git add"),
    (("git", "commit"),        "shell:scope:git commit"),
    (("git", "push"),          "shell:scope:git push"),
    # ...
]

# 命中 scope → 返回 scope key
# 未命中 → 返回 "shell:exact:{normalized_command}"
```

**问题**：`git add . && git commit -m 'msg' && git push` 包含连词，整体变成 exact 匹配，无法复用 scope。

## 优化方案：分割检查 + 变量引用检测

```python
def check_command_split_first(command: str) -> Decision:
    # 1. 按逻辑连词分割
    parts = split_by_operators(command)  # ;, &&, ||, |, &
    
    # 2. 逐个检查
    for sub_cmd in parts:
        if not is_subcommand_safe(sub_cmd):
            return check_command_whole(command)  # 回退整体检查
    
    # 3. 全部 SAFE → 直接放行
    return ALLOW

def is_subcommand_safe(cmd: str) -> bool:
    # 关键：包含变量引用 → 不安全
    if re.search(r'\$\w+|\$\{[^}]+\}|\$\([^)]+\)|`[^`]+`', cmd):
        return False
    # 包含 eval/exec → 不安全
    if re.search(r'\b(eval|exec)\b', cmd):
        return False
    # 包含危险关键字 → 不安全
    if re.search(r'\brm\s+-rf?\b|\bmkfs\b|\bdd\b', cmd):
        return False
    # 匹配已知 safe 模式 → 安全
    return bool(re.match(r'^(echo|git\s+(add|commit|push)|npm\s+install)\b', cmd))
```

**关键防御规则**：
1. 包含 `$VAR`、`${VAR}`、`$(cmd)`、`` `cmd` `` 的子命令一律不视为 SAFE
2. 包含 `eval`/`exec` 的子命令一律不视为 SAFE
3. 只有纯文本且匹配已知 safe 模式的命令才视为 SAFE
4. 任何不确定的情况都回退到整体检查

**危险反例**（必须拦截）：
- `A=rm; B=-rf; C=/; $A $B $C` — 变量引用被检测到 → 拦截 ✓
- `cd /home && rm -rf .` — `rm -rf` 被检测到 → 拦截 ✓
- `false && rm -rf / || echo ok` — `rm -rf /` 被检测到 → 拦截 ✓

---

# 正则安全规则绕过审计方法论

当评估基于正则的命令拦截系统（如审批硬阻断）时，使用此方法论。

## 核心洞察

**正则匹配原始文本 ≠ Shell 执行语义。** Shell 在执行时做变量展开、路径规范化、引号剥离，这些正则都看不到。

## 绕过类别清单

| 类别 | 绕过示例 | 原理 | 防御 |
|------|---------|------|------|
| **路径等价** | `rm -rf /./`、`rm -rf //` | `/./` = `/` 但正则不匹配 | 路径规范化预处理 |
| **变量赋值+使用** | `X=/; rm -rf $X` | 正则无法展开 `$X` | 检测变量引用 |
| **引号包裹** | `rm -rf '/'` | 正则看到 `'/'` 不是 `/` | 引号剥离预处理 |
| **eval 包装** | `eval 'rm -rf /'` | 正则看到 eval 不是 rm | 检测 eval 关键字 |
| **命令替换** | `rm -rf $(echo /)` | 正则无法展开 `$(...)` | 检测命令替换 |
| **函数名变体** | `f(){ f\|f& };f` | fork bomb 换名不匹配 | 匹配函数定义模式 |
| **通配符** | `rm -rf /?` | glob 展开为 `/a /b /c` | 检测通配符 |
| **符号链接** | `ln -s /home /tmp/h && rm -rf /tmp/h` | 通过链接间接删除 | 需要运行时检测 |
| **完整路径** | `/usr/sbin/shutdown -h now` | `_CMDPOS` 不匹配完整路径前缀 | 匹配完整路径 |
| **find -exec** | `find / -exec rm -rf {} +` | 不在 hardline 里 | 添加 find 模式 |

## 测试流程

```python
def test_hardline_bypass(cmd, expected_blocked=True):
    is_blocked = detect_hardline_command(cmd)
    bypassed = not is_blocked and expected_blocked
    if bypassed:
        print(f"⚠ BYPASS: {cmd!r}")
    return bypassed

# 测试用例分类
test_cases = [
    # 路径变形
    ("rm -rf /.",          True),   # trailing dot
    ("rm -rf //",          True),   # double slash
    ("rm -rf /tmp/../",    True),   # parent traversal
    
    # 变量展开
    ("X=/; rm -rf $X",     True),   # variable assignment
    ("rm -rf $(echo /)",   True),   # command substitution
    
    # 引号
    ("rm -rf '/'",         True),   # single-quoted
    ('rm -rf "/"',         True),   # double-quoted
    
    # eval
    ("eval 'rm -rf /'",    True),   # eval wrapping
    
    # 函数名变体
    ("f(){ f|f& };f",      True),   # different function name
]
```

## 关键教训

1. **正则只能做"静态文本匹配"**，无法理解 shell 的动态语义
2. **防御变量引用比防御变量展开更实际** — 检测 `$VAR` 比展开 `$VAR` 简单得多
3. **硬阻断不是真正的"不可绕过"** — 只是提高了攻击成本
4. **最安全的方案是容器隔离**（seccomp/sandbox），不是正则匹配

---

# 会话管理设计

## 当前问题：空会话堆积

**现状**：每次进入 Navi CLI 界面（不带 `--resume`）就创建新会话，用户没说话也产生空会话记录。

**根因**：`AgentRuntime.__init__` 中立即创建 `HistoryStore`。

## 优化方案：延迟创建 HistoryStore

```
__init__:
  ├── resume_session_id 有值 → 立即创建 HistoryStore（保持不变）
  └── resume_session_id 为空 → 不创建，只保存参数
        │
        ▼
run_turn 第一次调用时:
  └── _ensure_session() → 检测到 session_store 为空 → 创建 HistoryStore
```

### 实现方式：Property 延迟初始化

```python
class AgentRuntime:
    def __init__(self, ..., resume_session_id=None):
        self._history_db_path = history_db_path
        self._session_store: HistoryStore | None = None
        
        if resume_session_id:
            self._session_store = HistoryStore.from_existing(history_db_path, resume_session_id)
        # else: 不创建，等用户说话时再创建
    
    @property
    def session_store(self) -> HistoryStore:
        if self._session_store is None:
            self._session_store = HistoryStore(
                db_path=self._history_db_path,
                project_path=str(self.workspace),
                provider=self._default_provider,
                model=self._default_model,
            )
        return self._session_store
```

**好处**：外部代码仍然用 `runtime.session_store`，不需要改调用方。

### 需要改动的地方

1. `__init__`：新会话时不创建 `HistoryStore`，改为 `self._session_store = None`
2. 添加 `session_store` property，内部调用延迟创建
3. `run_turn` 入口处确保会话已创建（访问 `self.session_store` 即可）
4. `splash` 显示 model 信息：从 config 读取，不需要 runtime
5. `key_bindings` 需要 runtime：可以传 None，后续更新
6. `bottom_toolbar` 需要 runtime：处理 None 的情况

---

# 工具列表与模型上下文

## 核心概念：tools 参数

工具列表通过 API 请求的 `tools` 参数传给模型，和 `messages` **平级**：

```json
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "You are a helpful assistant..."},
    {"role": "user", "content": "帮我查一下 docker 的用法"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "read_file",
        "description": "读取文本文件内容",
        "parameters": {...}
      }
    }
  ]
}
```

**每个工具的 schema（name + description + parameters）约占 100-200 token。** 工具越多，token 消耗越大。

## Prompt Caching 影响

prompt caching（OpenAI、Anthropic 都支持）基于**前缀匹配**。如果 `tools` 参数变了，前缀不一致，缓存失效。

**风险**：
- 工具列表**没变** → 前缀一致 → 缓存命中 ✓
- 工具列表**变了** → 前缀不一致 → 缓存失效 ✗

**优化策略**：只在工具列表真正变化时才更新，避免不必要的缓存失效。

---

# MCP 工具生命周期管理

## 核心概念：两个列表

Navi 有两层工具容器，职责不同：

| 容器 | 类型 | 内容 | 更新时机 |
|------|------|------|---------|
| `tool_registry` | ToolRegistry 实例 | 所有已注册工具的 handler | 启动时 + `/mcp reload` |
| `_tools_for_api` | list[dict] | 发给模型的 OpenAI tools schema | 仅 `__init__` 时构建一次 |

**关键区别**：
- `tool_registry` 是运行时实际可执行的工具集合
- `_tools_for_api` 是告诉模型"你有哪些工具"的声明
- 模型只能调用 `_tools_for_api` 中声明的工具
- 但实际执行时去 `tool_registry` 中找 handler

## 当前设计的问题

### 问题 1：Resume 时新 MCP 工具不可见

```python
# agent.py 第 194-200 行
if resume_session_id and persisted_tool_names:
    allowed = set(persisted_tool_names)
    self._tools_for_api = [t for t in all_tools if t.name in allowed]
```

Resume 时用 `persisted_tool_names`（上次会话记录）过滤。新注册的 MCP 工具被过滤掉，模型看不到。

### 问题 2：`/mcp reload` 后工具仍不可用

`reload_mcp_servers(registry)` 只更新 `tool_registry`，不更新 `_tools_for_api`。模型仍然看不到新工具。

### 问题 3：MCP 连接失败时工具完全消失

连接失败时工具不注册到 `tool_registry`，也不出现在 `_tools_for_api`。用户不知道有这个工具。

## Claude Code 的做法

### 1. 每轮请求前刷新工具列表

```typescript
// query.ts 第 1659-1668 行
if (updatedToolUseContext.options.refreshTools) {
  const refreshedTools = updatedToolUseContext.options.refreshTools()
  if (refreshedTools !== updatedToolUseContext.options.tools) {
    updatedToolUseContext = {
      ...updatedToolUseContext,
      options: {
        ...updatedToolUseContext.options,
        tools: refreshedTools,  // 直接更新 tools 参数
      },
    }
  }
}
```

**每轮请求前**，调用 `refreshTools()` 获取最新工具列表，如果有变化就更新。模型下一轮请求就能看到新工具。

### 2. 监听 MCP `tools/list_changed` notification

```typescript
// useManageMCPConnections.ts 第 620-663 行
client.client.setNotificationHandler(
  ToolListChangedNotificationSchema,
  async () => {
    const newTools = await fetchToolsForClient(client)
    updateServer({ ...client, tools: newTools })
  }
)
```

### 3. 用 `<system-reminder>` 通知工具变化

在 messages 中插入一条系统提醒，告诉模型"工具可用/不可用了"。

## Navi vs Claude Code 对比

| 特性 | Navi | Claude Code |
|------|------|-------------|
| 工具列表更新时机 | `__init__` 时构建一次 | 每轮请求前刷新 |
| Resume 时工具过滤 | 基于 `persisted_tool_names` | 不过滤，基于当前 registry |
| MCP 重连后 | 必须开新会话 | 下一轮请求自动可用 |
| 工具变化通知 | 无 | `<system-reminder>` 标签 |
| 缓存影响 | 稳定（不变） | 可能失效（但有优化） |

## 新设计方案（用户提出）

**核心思路：工具列表基于配置，不基于连接状态。**

```
当前设计：_tools_for_api = 实际注册成功的工具
新设计：  _tools_for_api = 配置中启用的工具（不管连接状态）
```

### 改动点

1. **`__init__` 中**：`_tools_for_api` 包含配置中所有启用的 MCP 工具（即使未连接）
2. **MCP 连接失败时**：注册占位 handler 到 `tool_registry`，调用时返回错误信息
3. **`/mcp reload` 后**：更新 `_tools_for_api`（合并新工具）
4. **Resume 时**：以当前配置为准，新工具加上，删掉的工具移除

### 权衡

| 方案 | 好处 | 坏处 |
|------|------|------|
| 工具列表基于配置 | MCP 重连后立即可用 | 不可用时浪费 token、误导模型 |
| 工具列表基于连接状态 | 不浪费 token、不误导 | MCP 重连后不可用，必须开新会话 |
| 每轮请求前刷新（Claude Code） | MCP 重连后立即可用 | 可能影响缓存 |

### MCP 工具调用失败的错误处理

现有代码（`mcp_client.py` 第 455-461 行）已有机制：

```python
if not server or not server.session:
    return json.dumps({
        "error": f"MCP server '{server_name}' is not connected"
    }, ensure_ascii=False)
```

配套电路断路器（circuit breaker）：连续失败 N 次后暂停调用，冷却后自动恢复。

---

# 会话搜索工具对比分析（Navi vs Hermes）

## 架构对照

| 组件 | Navi | Hermes |
|------|------|--------|
| 工具入口 | `tools/builtin.py` SearchSessionTool 类 | `tools/session_search_tool.py` 独立模块 |
| 存储层 | `storage/history_store.py` HistoryStore | `hermes_state.py` SessionDB |
| 三种模式 | DISCOVERY / SCROLL / BROWSE ✅ | 同上 ✅ |

## 发现的问题

### 问题 1（严重）：`_sanitize_fts5_query` 过度引号化

**Navi**（第 833-838 行）把所有含字母/数字的术语都包上引号：

```python
sanitized = re.sub(r"[\w][\w\-\.]*[\w]", _quote_term, sanitized)
```

`docker deployment` → `"docker" "deployment"` — 变成短语匹配（要求相邻），而非默认 AND 匹配。

**Hermes**（第 2115 行）只引号化含 `-` / `.` / `_` 的术语：

```python
sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
```

**影响**：Navi 搜索召回率显著低于 Hermes。

### 问题 2（中等）：FTS5 索引内容不完整

**Hermes** 触发器索引 `content + tool_name + tool_calls`：

```sql
INSERT INTO messages_fts(rowid, content) VALUES (
    new.id,
    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, '')
);
```

**Navi** 只索引 `content_text`。搜索不到 tool_name 和 tool_calls 中的关键词。

### 问题 3（中等）：SCROLL 缺少 lineage rebind

Hermes 的 `_scroll` 有 lineage rebind 机制：当 `session_id + around_message_id` 找不到消息时，查找消息的真实 owning session，如果属于同一 lineage 则透明重绑定。

Navi 的 `_scroll` 没有这个机制，直接返回错误。

### 问题 4（轻微）：DISCOVERY 默认不过滤 role

Hermes 默认只搜 `["user", "assistant"]`，排除 tool 输出噪音。Navi 不传 role_filter，搜所有角色。

### 问题 5（轻微）：FTS5 表有冗余 UNINDEXED 列

Navi 的 FTS5 表包含 `session_id UNINDEXED, role UNINDEXED, title UNINDEXED, project_path UNINDEXED`，不参与索引，只增加存储开销。Hermes 只有 `content` 一列。

## Navi 做得好的地方

- 三种模式设计完整复刻 Hermes
- `_resolve_to_root` lineage 解析逻辑正确
- `get_anchored_view` 三层切片（window + bookend_start + bookend_end）实现正确
- CJK 三路分派（trigram / LIKE / 标准 FTS5）完全复刻
- BROWSE 排除有 parent_session_id 的子会话
- `for_querying` 只读构造器避免搜索时创建新 session

---

# 分析开源项目工具实现的方法论

当用户询问某个开源项目的工具实现细节时（如"XX项目的YY工具是怎么实现的"），使用以下方法论。

## 分析流程

### 1. 定位工具定义

工具通常位于以下位置：
- `src/tool/` 或 `tools/` 目录
- 文件名模式：`{tool-name}.ts`、`{tool_name}.py`、`tool.ts`、`tool.py`

**快速定位技巧**：
```bash
# 搜索工具定义
grep -r "Tool.define\|@tool\|def.*tool" --include="*.ts" --include="*.py"

# 查看工具目录
ls -la src/tool/ 或 tools/
```

### 2. 分析参数定义

工具参数定义通常使用：
- TypeScript: `z.object({...})` (Zod schema)
- Python: `@tool` 装饰器或 `Parameters` 类

**关键参数类型**：
- `z.string()` - 字符串
- `z.number()` - 数字
- `z.boolean()` - 布尔值（常用于开关功能）
- `.optional()` - 可选参数
- `.describe()` - 参数描述（包含使用说明）

### 3. 追踪参数使用

找到参数定义后，追踪其在代码中的使用：
```bash
# 搜索参数使用
grep -n "params.interactive" src/tool/bash.ts
grep -n "params.timeout" src/tool/bash.ts
```

### 4. 理解底层服务

工具通常调用底层服务，这些服务位于：
- 同目录下的其他文件（如 `bash-interactive.ts`）
- `src/service/` 或 `src/core/` 目录
- 独立模块（如 `@/shell/shell`）

## MiMo-Code bash 工具分析示例

### 工具位置
- `packages/opencode/src/tool/bash.ts`

### 关键参数
```typescript
const Parameters = z.object({
  command: z.string().describe("The command to execute"),
  timeout: z.number().describe("Optional timeout in milliseconds").optional(),
  workdir: z.string().describe("Working directory").optional(),
  interactive: z.boolean().describe("Set to true when the command requires user interaction").optional(),
  description: z.string().describe("Clear, concise description of what this command does"),
})
```

### 交互模式实现
当 `interactive: true` 时：
1. 调用 `BashInteractive.request()` 服务
2. 通过事件总线（Bus）发布 `bash.interactive.asked` 事件
3. 等待用户交互（终端控制权转移）
4. 收到 `bash.interactive.replied` 事件后返回结果

### Shell 选择逻辑
Windows 优先级：`pwsh` > `powershell` > `git bash` > `cmd.exe`
macOS：`/bin/zsh`
Linux：`bash` > `/bin/sh`

**Git Bash 检测**：
```typescript
export function gitbash() {
  if (process.platform !== "win32") return
  if (Flag.MIMOCODE_GIT_BASH_PATH) return Flag.MIMOCODE_GIT_BASH_PATH
  const git = which("git")
  if (!git) return
  const file = path.join(git, "..", "..", "bin", "bash.exe")
  if (Filesystem.stat(file)?.size) return file
}
```

## 常见工具模式

### 1. 交互式命令模式
- 参数：`interactive: boolean`
- 实现：事件总线 + Deferred/Promise
- 场景：密码输入、y/N 确认、SSH 密钥

### 2. 超时控制模式
- 参数：`timeout: number`
- 实现：`setTimeout` + 进程终止
- 场景：长时间运行的命令

### 3. 工作目录模式
- 参数：`workdir: string` 或 `cwd: string`
- 实现：`child_process.spawn` 的 `cwd` 选项
- 场景：在特定目录执行命令

### 4. 环境变量模式
- 参数：`env: Record<string, string>`
- 实现：合并到 `process.env`
- 场景：自定义环境变量

---

# LangGraph 功能评估框架

当用户询问"哪些功能可以用 LangGraph 内置功能替代手写实现"时，使用以下评估框架。

## 评估维度

| 维度 | 问题 | 决策依据 |
|------|------|---------|
| 模型兼容性 | 目标模型是否支持 `bind_tools()`？ | 不支持则需要手动构造 tool_calls |
| 状态管理 | 是否需要跨轮次持久化？ | 需要则考虑 MessagesState + Checkpoint |
| 人类交互 | 是否需要 Human-in-the-loop？ | 需要则考虑 interrupt 机制 |
| 可视化 | 是否需要工作流可视化？ | 需要则用子图而非手写循环 |
| 流式执行 | 是否需要流式输出？ | LangGraph 内置流式支持 |

## LangGraph 内置功能清单

| 功能 | 适用场景 | 限制 |
|------|---------|------|
| **ToolNode** | 执行工具调用 | 需要标准格式的 tool_calls 消息 |
| **MessagesState** | 需要消息列表管理 | 内置 `add_messages` reducer |
| **子图（Subgraph）** | 多 Agent 系统、可复用工作流 | 需要定义输入/输出 schema |
| **Human-in-the-loop** | 需要用户交互、审批流程 | 需要 CheckpointSaver |
| **条件边** | 动态路由、工具调用判断 | 已使用，可优化 |
| **create_react_agent** | 标准 ReAct 模式 | 仅适用于支持工具调用的模型 |

## 关键发现：ToolNode 不需要 bind_tools()

**ToolNode 只需要两样东西**：
1. 工具列表（LangChain Tool 对象或 @tool 装饰的函数）
2. 消息中正确格式的 `tool_calls`

**这意味着**：你可以保留完全自定义的 LLM 调用和 tool_calls 构造逻辑，只用 ToolNode 替换工具执行部分。

```python
# 保留自定义的 _llm_node（处理流式响应、reasoning_content 等）
def _llm_node(state):
    # 手动解析流式响应，构造标准 tool_calls 格式
    assistant_message = {
        "role": "assistant",
        "content": content,
        "tool_calls": [
            {
                "id": "call_xxx",
                "type": "function",
                "function": {"name": "tool_name", "arguments": '{"param": "value"}'}
            }
        ]
    }
    return {"messages": [*state["messages"], assistant_message]}

# 使用 ToolNode 替换手写的工具执行逻辑
from langgraph.prebuilt import ToolNode
tool_node = ToolNode([my_tool_1, my_tool_2])
```

**迁移收益**：
- 自动并行执行多个工具调用
- 自动构造 ToolMessage
- 内置错误处理
- 减少约 60 行手写代码

## 工具调用兼容性决策树

```
模型是否支持 bind_tools()？
├── 是 → 使用 ToolNode + llm.bind_tools()
│       └── 自动处理并行执行、错误处理、状态注入
└── 否 → 手动解析 tool_calls
        └── 使用 ToolNode + 手动构造消息
            └── 在 llm_node 中手动解析并构造标准 tool_calls 格式
            └── ToolNode 只负责执行，不关心 tool_calls 如何产生
```

## 模型工具调用支持情况

| 模型类型 | bind_tools 支持 | tool_calls 格式 | 适配方案 |
|---------|----------------|-----------------|---------|
| OpenAI/Anthropic 原生 | ✅ 完全支持 | 标准 JSON | 直接使用 ToolNode + bind_tools |
| OpenAI-compatible（如 MiMo） | ⚠️ 部分支持 | 需要验证 | 手动解析 + ToolNode |
| 国内模型（通义、文心等） | ❌ 多数不支持 | 自定义格式 | 手动解析 + 格式转换 + ToolNode |
| 开源模型（LLaMA、Qwen） | ⚠️ 取决于版本 | 可能需要特殊 prompt | 手动解析 + prompt 工程 + ToolNode |

## MiMo 模型适配经验

MiMo 使用 OpenAI-compatible API，支持 `tools` 参数，但流式响应中的 `tool_calls` 需要手动累积：

```python
# llm_client.py 中的调用方式
stream = client.chat.completions.create(
    model=MIMO_MODEL,
    messages=messages,
    tools=tools,  # 传入 tools 参数
    stream=True,
    extra_body={"thinking": {"type": "enabled"}},  # MiMo 特有参数
)

# 手动解析 tool_calls（当前实现）
for chunk in stream:
    delta = chunk.choices[0].delta
    if delta.tool_calls:
        for tc in delta.tool_calls:
            # 手动累积 id、name、arguments
            tool_calls_map[idx]["id"] += tc.id
            tool_calls_map[idx]["function"]["name"] += tc.function.name
            tool_calls_map[idx]["function"]["arguments"] += tc.function.arguments
```

## 外部依赖注入模式

当工具需要访问外部资源（如数据库连接）时，使用闭包注入：

```python
def create_tools(db_conn):
    @tool
    def search_history(query: str) -> dict:
        """搜索历史。"""
        hits = search_messages(db_conn, query=query)
        return {"ok": True, "matches": hits}
    
    return [search_history]

# 使用
tools = create_tools(db_conn)
tool_node = ToolNode(tools)
```

## 替代方案评估模板

当评估是否用 LangGraph 内置功能替代手写代码时：

```markdown
### 当前实现
- 文件：xxx.py
- 行数：xxx 行
- 功能：xxx

### LangGraph 内置方案
- 功能：xxx
- 依赖：是否需要 bind_tools()？
- 兼容性：目标模型是否支持？

### 决策
- [ ] 使用内置方案（原因：xxx）
- [ ] 保留手写实现（原因：xxx）
- [ ] 混合方案（原因：xxx）← ToolNode 通常走这条路
```

---

# MCP 集成架构（Hermes 参考实现）

## 概念

MCP（Model Context Protocol）是 Anthropic 提出的开放协议，用于标准化 AI 模型与外部工具/数据源的交互。架构：客户端（AI 应用） ↔ MCP Server（工具/数据源）。

## Hermes MCP 模块结构

```
hermes-agent/
├── tools/
│   ├── mcp_tool.py (156KB)      # 核心：连接管理、工具发现、注册、调用、重连
│   ├── mcp_oauth.py             # OAuth 2.1 认证
│   └── mcp_oauth_manager.py     # OAuth token 管理
├── hermes_cli/
│   ├── mcp_config.py            # CLI 子命令：hermes mcp add/remove/list/test
│   ├── mcp_catalog.py           # 预置 MCP 服务器目录
│   └── mcp_picker.py            # 交互式选择器
└── config.yaml                  # mcp_servers 配置段
```

## 配置格式

```yaml
# ~/.hermes/config.yaml
mcp_servers:
  # stdio 传输（本地进程）
  filesystem:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    env: {}
    timeout: 120
    connect_timeout: 60

  # HTTP 传输（远程服务）
  remote_api:
    url: "https://my-mcp-server.example.com/mcp"
    headers:
      Authorization: "Bearer sk-..."

  # SSE 传输
  searxng:
    url: "http://localhost:8000/sse"
    transport: sse

  # 工具过滤
  github:
    command: "npx"
    args: ["-y", "@modelcontextprotocol/server-github"]
    tools:
      include: [create_issue, list_issues]  # 白名单
      exclude: [delete_customer]            # 黑名单
    supports_parallel_tool_calls: true      # 允许并行调用
    enabled: true                           # 可禁用
```

## 核心架构

```
┌─────────────────────────────────────────────────────────────┐
│                        Hermes CLI                           │
├─────────────────────────────────────────────────────────────┤
│  config.yaml                    tools/mcp_tool.py           │
│  ┌──────────────┐              ┌──────────────────────┐    │
│  │ mcp_servers:  │              │  Background EventLoop │    │
│  │   github:     │──────────────│  (daemon thread)      │    │
│  │     command:  │              │                       │    │
│  │     args:     │              │  MCPServerTask × N    │    │
│  │   filesystem: │              │    ├── stdio          │    │
│  │     command:  │              │    ├── http/sse       │    │
│  └──────────────┘              │    └── reconnect      │    │
│                                └──────────┬───────────┘    │
│                                           │                 │
│  tools/registry.py  ◄─────────────────────┘                │
│  (工具注册表，MCP 工具名格式: mcp_{server}_{tool})            │
└─────────────────────────────────────────────────────────────┘
```

## 关键函数（Hermes mcp_tool.py）

| 函数 | 行号 | 职责 |
|------|------|------|
| `discover_mcp_tools()` | 3400 | 入口：加载配置、连接服务器、注册工具 |
| `register_mcp_servers()` | 3305 | 连接显式配置的 MCP 服务器并注册工具 |
| `_discover_and_register_server()` | 3276 | 连接单个服务器、发现工具、注册 |
| `_register_server_tools()` | 3166 | 将工具注册到 registry（含 include/exclude 过滤)|
| `_connect_server()` | 2386 | 建立连接（stdio/http/sse）|
| `shutdown_mcp_servers()` | 3573 | 关闭所有连接、停止后台 loop |
| `probe_mcp_server_tools()` | 3507 | 临时连接探测工具列表（不注册）|
| `get_mcp_status()` | 3467 | 返回所有服务器状态（用于 banner）|
| `is_mcp_tool_parallel_safe()` | 3449 | 检查工具是否允许并行调用 |

## 关键特性

- **后台事件循环**：专用 daemon thread 运行 asyncio loop，不阻塞 CLI
- **并行连接**：多个 MCP server 同时连接（`asyncio.gather`）
- **自动重连**：指数退避，最多 5 次重试
- **工具过滤**：include/exclude 白名单/黑名单
- **安全**：环境变量过滤、错误信息中的凭证脱敏
- **热重载**：`/reload-mcp` 命令 + config.yaml 文件监控自动重载
- **Sampling**：MCP server 可请求 LLM 补全（双向通信）
- **OAuth**：支持 OAuth 2.1 + PKCE 认证

## 工具注册流程

```
discover_mcp_tools()
  → _load_mcp_config()           # 读取 config.yaml
  → register_mcp_servers()       # 连接 + 注册
    → _ensure_mcp_loop()         # 启动后台 event loop
    → asyncio.gather(...)        # 并行连接所有 server
      → _connect_server()        # 建立连接
      → _register_server_tools() # 注册工具到 registry
        → registry.register()    # 名称格式: mcp_{server}_{tool}
```

---

## Navi MCP 集成方案

### 需要新增的文件

| 文件 | 职责 | 大小估计 |
|------|------|---------|
| `mcp_client.py` | MCP 客户端核心：连接管理、工具发现、调用 | ~500 行 |
| `mcp_config.py` | 配置读写、服务器管理 CLI | ~200 行 |

### 需要修改的文件

| 文件 | 修改内容 |
|------|---------|
| `config.json` | 新增 `mcp_servers` 字段 |
| `tool_registry.py` | 支持动态注册/注销（当前只有 register，没有 unregister)|
| `runtime.py` | 启动时调用 `discover_mcp_tools()`，关闭时调用 `shutdown_mcp_servers()` |
| `cli.py` | 新增 `/mcp` 命令（add/remove/list/reload）|

### mcp_client.py 核心函数

```python
# 后台事件循环管理
_ensure_mcp_loop() -> None          # 启动 daemon thread + asyncio loop
_stop_mcp_loop() -> None            # 停止 loop
_run_on_mcp_loop(coro, timeout)     # 在后台 loop 上执行协程

# 服务器连接
async _connect_server(name, config) -> MCPServerTask
async _disconnect_server(name) -> None

# 工具发现与注册
discover_mcp_tools() -> List[str]           # 入口：加载配置、连接、注册
register_mcp_servers(servers) -> List[str]  # 连接并注册
shutdown_mcp_servers() -> None              # 关闭所有连接

# 工具调用
call_mcp_tool(server_name, tool_name, arguments) -> dict

# 状态查询
get_mcp_status() -> List[dict]
probe_mcp_server_tools() -> Dict[str, List[tuple]]
```

### config.json 扩展

```json
{
  "mcp_servers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
      "enabled": true,
      "timeout": 120
    },
    "github": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-github"],
      "env": {
        "GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"
      },
      "tools": {
        "include": ["create_issue", "list_issues"]
      }
    }
  }
}
```

### tool_registry.py 扩展

```python
def unregister(self, name: str) -> bool:
    """注销工具，返回是否成功。"""
    if name not in self._tools:
        return False
    del self._tools[name]
    return True

def unregister_by_prefix(self, prefix: str) -> int:
    """注销所有以 prefix 开头的工具，返回注销数量。"""
    to_remove = [n for n in self._tools if n.startswith(prefix)]
    for name in to_remove:
        del self._tools[name]
    return len(to_remove)
```

### 实现优先级

1. **Phase 1：基础连接** — `mcp_client.py` + stdio 传输 + 工具注册
2. **Phase 2：CLI 集成** — `/mcp` 命令 + config.json 配置
3. **Phase 3：高级特性** — HTTP/SSE 传输 + 自动重连 + 工具过滤
4. **Phase 4：生产级** — OAuth + Sampling + 热重载

---

# SQLite + FTS5 会话搜索架构（Hermes 参考实现）

## 概述

Hermes 使用 SQLite + FTS5 实现跨会话全文搜索，支持拉丁语系和 CJK（中日韩）字符。

## 数据模型

```sql
-- 主表：消息存储
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    tool_name TEXT DEFAULT '',
    tool_calls TEXT DEFAULT '',
    timestamp TEXT DEFAULT (datetime('now'))
);

-- FTS5 虚拟表：unicode61 分词（拉丁语系）
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    tokenize='unicode61'
);

-- FTS5 虚拟表：trigram 分词（CJK 支持）
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'
);

-- 自动同步触发器
CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, ''));
END;
```

## CJK 三路分发策略

```
查询输入
    │
    ├─ 包含 CJK？
    │   ├─ ≥3 CJK 字符 → trigram FTS5（精确子串匹配）
    │   └─ <3 CJK 字符 → LIKE 回退（全表扫描）
    │
    └─ 非 CJK → 标准 FTS5（unicode61 分词）
```

### Trigram 原理

```
文档: "大别山项目" (每个汉字 3 字节 UTF-8)

Trigram 分词 (9 字节窗口, 滑动 1 字节):
  "大别山"  → [大₁大₂大₃别₁别₂别₃山₁山₂山₃]
  "别山项"  → [别₁别₂别₃山₁山₂山₃项₁项₂项₃]
  "山项目"  → [山₁山₂山₃项₁项₂项₃目₁目₂目₃]

查询: "别山" (6 字节 < 9 字节) → ❌ 无法匹配
查询: "别山项" (9 字节) → ✅ 精确子串匹配
```

## 核心 API

### search_messages() — 全文搜索

```python
def search_messages(
    self,
    query: str,
    limit: int = 20,
    offset: int = 0,
    sort: str = None,           # None | "newest" | "oldest"
    session_id: str = None,     # 可选：限定单个会话
    role_filter: list[str] = None,
) -> list[dict[str, Any]]:
    """FTS5 全文搜索，返回匹配消息 + ±1 上下文"""
```

返回结果示例：
```python
[
    {
        "id": 1234,
        "session_id": "20250608_abc123",
        "role": "user",
        "snippet": "...用 >>>docker<<< >>>部署<<<到生产环境...",
        "timestamp": "2025-06-08T10:30:00",
        "context": [
            {"role": "assistant", "content": "我来帮你部署..."},
            {"role": "user", "content": "用docker部署到生产环境"},
            {"role": "assistant", "content": "好的，先检查配置..."}
        ]
    },
    ...
]
```

### get_messages_around() — 上下文窗口

```python
def get_messages_around(
    self,
    session_id: str,
    around_message_id: int,
    window: int = 5,
) -> dict[str, Any]:
    """返回锚定消息 ± window 条上下文"""
```

返回结果示例：
```python
{
    "window": [
        {"id": 1230, "role": "user", "content": "怎么部署？"},
        {"id": 1232, "role": "assistant", "content": "用docker..."},
        {"id": 1234, "role": "user", "content": "docker 部署到生产"},  # 命中点
        {"id": 1236, "role": "assistant", "content": "好的..."},
        {"id": 1238, "role": "user", "content": "继续"},
    ],
    "messages_before": 2,
    "messages_after": 2
}
```

### get_anchored_view() — 锚定视图 + 首尾摘要

```python
def get_anchored_view(
    self,
    session_id: str,
    around_message_id: int,
    window: int = 5,
    bookend: int = 3,
) -> dict[str, Any]:
    """返回锚定窗口 + 会话首尾摘要"""
```

返回结果示例：
```python
{
    "bookend_start": [{"role": "user", "content": "帮我部署项目"}],      # 会话开头
    "window": [...],                                                      # 锚定窗口
    "bookend_end": [{"role": "assistant", "content": "部署完成"}],        # 会话结尾
    "messages_before": 2,
    "messages_after": 2
}
```

## 辅助方法

```python
@staticmethod
def _sanitize_fts5_query(query: str) -> str:
    """清洗 FTS5 查询，处理特殊字符"""
    # 保留引号包裹的短语
    # 转义 FTS5 特殊字符 +{}()\"^
    # 包裹含 - 或 . 的术语
    # 移除尾部悬空布尔运算符

@staticmethod
def _contains_cjk(text: str) -> bool:
    """检测是否包含 CJK 字符"""
    # Unicode 范围：CJK Unified Ideographs (0x4E00-0x9FFF)

@classmethod
def _count_cjk(cls, text: str) -> int:
    """统计 CJK 字符数"""
```

## 设计要点

1. **双 FTS5 表**：unicode61（拉丁语系）+ trigram（CJK）
2. **触发器同步**：INSERT/UPDATE/DELETE 自动同步 FTS5 表
3. **三路分发**：CJK ≥3 字符 → trigram，CJK <3 字符 → LIKE，非 CJK → unicode61
4. **上下文窗口**：搜索结果 ±N 条消息，理解完整语境
5. **首尾摘要**：bookend 机制，不用加载整个会话就能看到任务目标和结果

---

# Navi CLI 双渲染路径架构

Navi CLI 有两套渲染路径，根据运行模式选择：

## 交互模式（`_start_chat_async`）→ StreamingBox

- 文件：`stream_box.py`
- **纯 ANSI 输出**，不经过 Rich Markdown 渲染器
- 用 `_strip_markdown_syntax()` 正则去除 markdown 标记（`#`、`**`、`` ` ``、`[]()` 等）
- 通过 prompt_toolkit 的 `print_formatted_text(ANSI(...))` 输出，兼容 patch_stdout
- 工具事件（tool_start/tool_result）仍用 Rich 的 `Syntax` 做 diff/输出高亮，但最终转 ANSI 字符串
- 有边框的 reasoning/response box（`╭─ Navi ─╮` / `┌─ Reasoning ─┐`）

## 非交互模式（`run_task`）→ NaviStreamView

- 文件：`ui.py`
- 使用 Rich 全套：`Live`、`Markdown`、`Console`、`BulletColumns`
- 流式渲染，transient=True（刷新时清除上一帧）
- 有 `NaviInlineStreamState` 变体用于嵌入式场景

## 事件处理器接线

```
ChatController.__init__():
  runtime.event_handler = lambda e: self.print_agent_event(e, box=self.stream_box)
  runtime.on_output = self.print_live

run_with_stream_view() (task 模式):
  original_event_handler = runtime.event_handler  # 保存
  runtime.event_handler = view.handle_event       # 临时替换为 NaviStreamView
  runtime.on_output = view.handle_output
  try:
      return runner()
  finally:
      runtime.event_handler = original_event_handler  # 恢复
```

---

# 输入框 Viewport 与滚动机制

当用户输入多行或超长文本时，CLI agent 需要将输入区限制在有限高度内，并支持光标跟随滚动。

## Claude Code：字符偏移 Viewport

基于 Ink (React TUI)，输入组件 `TextInput.tsx` → `useTextInput.ts`。

### 核心参数

```typescript
// PromptInput.tsx
const MIN_INPUT_VIEWPORT_LINES = 3;
const PROMPT_FOOTER_LINES = 5;

// 全屏模式：输入区高度 ≈ 终端行数的一半减去 footer
const maxVisibleLines = isFullscreenEnvEnabled()
  ? Math.max(MIN_INPUT_VIEWPORT_LINES, Math.floor(rows / 2) - PROMPT_FOOTER_LINES)
  : undefined;  // 非全屏模式：无限制，自然增长
```

### Viewport 滚动逻辑

`useTextInput` hook 中，光标位置通过 viewport 偏移计算：

```typescript
// 输入状态中
cursorLine: cursorPos.line - cursor.getViewportStartLine(maxVisibleLines),
cursorColumn: cursorPos.column,
viewportCharOffset: cursor.getViewportCharOffset(maxVisibleLines),
viewportCharEnd: cursor.getViewportCharEnd(maxVisibleLines),
```

- `getViewportStartLine(maxVisibleLines)` → 返回当前 viewport 起始行号
- `getViewportCharOffset/End` → 返回 viewport 内可见的字符范围
- 渲染时只显示 `[viewportCharOffset, viewportCharEnd)` 范围内的文本
- 光标始终在 viewport 内，超出时自动滚动

### 渲染方式

`BaseTextInput.tsx` 直接用 `<Box>` + `<Text>` + `<Ansi>` 渲染，无原生滚动条。视觉上是纯文本平铺，用户通过光标上下移动触发 viewport 滚动。

## Hermes：prompt_toolkit TextArea 动态高度

基于 prompt_toolkit，使用 `widgets.TextArea`。

### 核心配置

```python
# cli.py
input_area = TextArea(
    height=Dimension(min=1, max=8, preferred=1),
    multiline=True,
    wrap_lines=True,
    # ...
)
```

### 动态高度计算

覆盖 `input_area.window.height` 为计算函数：

```python
def _input_height():
    doc = input_area.buffer.document
    prompt_width = max(2, get_cwidth(self._get_tui_prompt_text()))
    available_width = get_app().output.get_size().columns - prompt_width
    visual_lines = 0
    for line in doc.lines:
        line_width = get_cwidth(line)  # CJK 宽字符算 2
        visual_lines += max(1, -(-line_width // available_width))  # ceil division
    return min(max(visual_lines, 1), 8)  # 硬上限 8 行

input_area.window.height = _input_height
```

### 滚动行为

- 高度范围：1-8 行，根据内容自动伸缩
- 超过 8 行时，prompt_toolkit TextArea 内置滚动机制接管
- 光标超出可见区域时自动滚动，无独立滚动条 UI
- CJK 宽字符通过 `get_cwidth` 正确计算占用宽度
- `wrap_lines=True` 使超长行自动换行，换行计入 visual_lines

## 设计对比

| 维度 | Claude Code | Hermes |
|------|-------------|--------|
| 输入组件 | Ink `<Box>` + `useTextInput` hook | prompt_toolkit `TextArea` widget |
| 最大高度 | `floor(rows/2) - 5`（全屏）/ 无限（非全屏） | 硬上限 8 行 |
| 最小高度 | 3 行（全屏） | 1 行 |
| 滚动机制 | 字符偏移 viewport（自行实现） | prompt_toolkit 内置滚动 |
| CJK 支持 | Ink 自动处理 | `get_cwidth` 手动计算 |
| 自动换行 | Ink `wrap="truncate-end"` | `wrap_lines=True` |

## Navi 当前状态

使用 prompt_toolkit `PromptSession`（非全屏），输入区是单行 prompt，多行通过 Shift+Enter。无 viewport 管理，内容超出终端高度时依赖终端自身滚动。

---

# 流式渲染全链路（Hermes 参考实现）

Hermes 的 CLI 流式渲染是同类项目中最成熟的，以下是完整架构。

## 回调链路

```
API streaming delta
  ↓
agent._fire_stream_delta(text)          # run_agent.py:3282
  ├─ StreamingThinkScrubber.feed()      # 去除 <think> 等推理标签
  ├─ context_scrubber.feed()            # 去除 memory context span
  ├─ lstrip("\n") (仅首个 delta)
  └─ stream_delta_callback(text)        # → CLI._stream_delta()
       ↓
CLI._stream_delta(text)                 # cli.py:4231
  ├─ None → _flush_stream() + _reset_stream_state()   # turn boundary
  ├─ _stream_prefilt buffer + tag detection (open/close tags)
  ├─ reasoning block → _stream_reasoning_delta()
  └─ normal text → _emit_stream_text()
       ↓
CLI._emit_stream_text(text)             # cli.py:4371
  ├─ 首次文本 → 打开 response box header（╭─ Navi ─╮）
  ├─ _stream_buf += text
  ├─ 逐行处理：
  │   ├─ 表格行 → _stream_table_buf（攒批）
  │   ├─ 非表格行 → _strip_markdown_syntax() → _emit_one()
  │   └─ 表格结束 → _flush_table_buf() → realign_markdown_tables()
  └─ _emit_one(line) → _cprint(f"{_PAD}{line}")
```

## 关键源码位置（Hermes）

| 文件 | 行号 | 功能 |
|------|------|------|
| `run_agent.py` | 3282 | `_fire_stream_delta()` — think scrubber + context scrubber |
| `agent/think_scrubber.py` | 64 | `StreamingThinkScrubber` — 状态机去除推理标签 |
| `cli.py` | 4231 | `_stream_delta()` — 行缓冲 + reasoning tag 检测 |
| `cli.py` | 4371 | `_emit_stream_text()` — response box + 表格缓冲 |
| `cli.py` | 4177 | `_stream_reasoning_delta()` — reasoning box |
| `cli.py` | 1793 | `_strip_markdown_syntax()` — markdown 标记去除 |
| `cli.py` | 3551 | `_status_bar_display_width()` — get_cwidth |
| `cli.py` | 3614 | `_scrollback_box_width()` — box 宽度 |
| `cli.py` | 1845 | `_terminal_width_for_streaming()` — 表格可用宽度 |
| `agent/markdown_tables.py` | 全文 | `realign_markdown_tables()` — wcwidth-aware 表格对齐 |

---

# 工具设计原则：通用工具 vs 专用工具

当需要为 Agent 添加新功能时，选择通用工具还是专用工具？

## 决策框架

| 维度 | 通用工具 | 专用工具 |
|------|---------|---------|
| 灵活性 | 高（可组合） | 低（格式受限） |
| LLM 负担 | 需要规划组合 | 接口清晰 |
| 扩展性 | 好（新格式不需要新工具） | 差（每种格式一个工具） |
| 代码复杂度 | 低（少量原子工具） | 高（每个工具单独实现） |

## 结论：保留通用工具

**不要**为特定格式创建专用工具（如 `generate_markdown`, `generate_html`）。

**用通用工具组合**：
```python
# 生成 .md
write_file(path="notes.md", content="# 学习笔记\n\n...")

# 生成 .html
write_file(path="notes.html", content="<!DOCTYPE html>...")

# 生成 .pdf（先写 md，再转换）
write_file(path="notes.md", content="...")
bash(command="pandoc notes.md -o notes.pdf")

# 修改已有文件
patch_file(path="notes.md", old_text="旧内容", new_text="新内容")
```

## 核心原则

**工具提供原子能力，LLM 负责编排组合。**

通用工具集（4 个足够）：
- `read_file` - 读取
- `write_file` - 创建/覆盖（任意格式）
- `patch_file` - 局部修改
- `bash` - 执行 Bash 命令（转换格式、运行代码、验证结果）

---

# 代码极简原则

1. **先跑通再抽象** — 不要在首次编写时设计复杂类层次
2. **追杀死代码** — while 循环不执行第二轮？参数从未使用？字段永远不变？立即删除
3. **内联小函数** — 4 行函数只被 2 处使用 → 合并
4. **分离关注点** — LLM 客户端只管 API，工具 schema 放 agent_config
5. **不要预埋扩展点** — 需要时再加，成本极低
6. **文件扁平化** — <10 个 .py 直接平铺
7. **复用 HTTP 客户端** — 模块级单例，不每次 `new OpenAI()`。客户端 ≠ 上下文
8. **增强版 CLI > 全屏 TUI** — 用户明确偏好
9. **直接模仿 Navi 参考代码** — 用户明确说"直接模仿 NAVI"，不要凭文档/搜索结果推测
