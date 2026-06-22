# KIMI-CLI 架构参考

> 源码路径: `E:\light_agent\kimi-cli\src\kimi_cli\` | 语言: Python 3.12+ | 框架: asyncio + kosong + fastmcp

## 整体架构

```
CLI 入口 (cli/__init__.py, Typer)
  ↓
KimiCLI.create() (app.py)  →  加载 config、选择 LLM、构建 Runtime
  ↓
KimiSoul.run() (soul/kimisoul.py)  →  主 Agent 循环
  ↓
  ├── Context (soul/context.py)  — 对话历史 + checkpoint
  ├── KimiToolset (soul/toolset.py)  — 工具加载与执行
  ├── Approval (soul/approval.py)  — 审批流程
  ├── Compaction (soul/compaction.py)  — 上下文压缩
  ├── DynamicInjection (soul/dynamic_injection/)  — AFK/plan mode 注入
  └── Wire (wire/)  — 事件流协议
  ↓
UI (ui/shell/, ui/print/, ui/acp/)  — 消费 Wire 消息
```

## Agent Loop (核心设计)

`KimiSoul` 是主循环（`soul/kimisoul.py`）：
- **异步驱动**: 基于 asyncio，支持并发
- **LLM 框架**: `kosong` — 自研轻量 LLM 调用框架（类似 LiteLLM）
- **Wire 协议**: 循环内的所有事件（step begin/end, tool call, tool result, compaction）通过 `wire/` 模块发送到 UI 层
- **重试机制**: tenacity 库，指数退避
- **上下文压缩**: `SimpleCompaction` — 超过 token 阈值自动压缩历史消息
- **动态注入**: PlanMode、AFK 模式通过 `DynamicInjectionProvider` 注入系统提示

关键设计：
```python
# KimiSoul.run() 伪代码
while steps < max_steps:
    wire_send(StepBegin)
    messages = context.build() + dynamic_injections
    step_result = await llm.chat(messages, tools)
    if step_result.tool_calls:
        for tc in step_result.tool_calls:
            approval = await approve(tc)
            result = await toolset.handle(tc, approval)
            context.append(result)
    else:
        context.append(step_result.message)
        break  # 模型没有调用工具，循环结束
```

## 工具系统

**核心文件**: `soul/toolset.py` + `tools/`

工具加载链路：
```
agentspec.yaml  →  定义 tools: [import.path.ToClass]
     ↓
KimiToolset  →  动态 import + 实例化
     ↓
  ├── builtin tools (tools/agent/, tools/file/, tools/shell/, tools/web/, tools/todo/, tools/plan/, tools/think/, tools/dmail/, tools/background/)
  └── MCP tools (通过 fastmcp 加载)
```

**工具定义特点**:
- YAML 驱动: `src/kimi_cli/agents/` 下的 agent spec 定义工具列表
- 继承机制: spec 可以 `extend` 基础 spec
- 动态加载: import path 字符串 → `importlib.import_module` → 实例化
- 工具实例通过 `__call__` 执行，返回 `ToolOk | ToolError`
- 内建工具 ~15 个类别：agent, file, shell, web, todo, plan, think, dmail, background, ask_user 等

**Agent Spec 示例思路**（YAML）:
```yaml
name: default
tools:
  - kimi_cli.tools.shell.ShellTool
  - kimi_cli.tools.file.ReadTool
  - kimi_cli.tools.file.WriteTool
subagents:
  code-reviewer:
    type: builtin.code_reviewer
```

## 审批系统

三层设计（`soul/approval.py` + `approval_runtime/`）:

1. **ApprovalState** — 会话级状态:
   - `yolo`: 自动批准所有
   - `afk` / `runtime_afk`: 用户不在时的自动批准
   - `auto_approve_actions`: 特定 action 自动批准集合

2. **Approval** — 工具调用前审批:
   - `request()` → 等待用户决定 → `approve | approve_for_session | reject`
   - 支持带 feedback 的 reject

3. **ApprovalRuntime** — 跨 session 的审批源管理:
   - 区分审批来源（user, subagent）
   - 子 Agent 被拒绝时给出更详细的错误提示

## 会话管理

- **Session**: `src/kimi_cli/session.py` — session 元数据和持久化
- **Context**: `soul/context.py` — 对话消息列表 + checkpoint 机制
- **SubagentStore**: `subagents/store.py` — 子 Agent 实例持久化（prompt、日志、上下文）
- 存储位置: session 目录下 `subagents/<agent_id>/`

## Subagent 系统

核心组件：
- **Agent tool** (`tools/agent/`): 创建或恢复 subagent 实例
- **LaborMarket** (`subagents/registry.py`): 注册 builtin subagent 类型
- **SubagentStore**: 持久化 subagent 状态
- **ToolPolicy**: 控制 subagent 可以使用的工具

## Wire 消息协议

`wire/` 模块定义标准化事件类型：
- `TurnBegin/End`, `StepBegin/End`, `StepInterrupted`, `StepRetry`
- `ToolCall`, `ToolResult`, `ToolCallRequest`
- `CompactionBegin/End`, `StatusUpdate`
- `TextPart`, `ImageURLPart`, `ContentPart`

UI 层（shell/print/acp）统一消费 Wire 消息，实现 UI 与逻辑的解耦。

## 技能系统

- 标准技能: `/skill:<name>` 加载 `SKILL.md` 作为用户提示
- Flow 技能: `/flow:<name>` 执行嵌入的流程（有向图：node + edge + choice）
- 技能发现: `discover_skills_from_roots()` 从多个根目录扫描

## 对 navi 有价值的模式

1. **Wire 消息协议**: UI 与 Agent 循环解耦，同一个 Agent 可以对接 Shell/Print/ACP 多种 UI
2. **YAML Agent Spec**: 声明式定义 Agent 的工具集、subagent 类型，比硬编码灵活
3. **DynamicInjection**: Plan mode、AFK 等模式通过 provider 注入系统提示，不改动核心循环
4. **Context + checkpoint**: 支持对话历史的保存和恢复
5. **ToolPolicy**: 精细控制 subagent 的工具权限
6. **Flow skills**: 技能不仅可以是文本注入，还可以是带节点的有向流程图
7. **Compaction**: 自动检测上下文长度并压缩，防止 token 超限
