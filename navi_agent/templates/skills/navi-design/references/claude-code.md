# Claude Code 架构参考

> 源码路径: `E:\light_agent\cc_src\src\` | 语言: TypeScript | UI: Ink (React TUI)

## 整体架构

```
entrypoints/  →  main.tsx  →  App.tsx  →  screens/ (REPL.tsx, Doctor.tsx, ResumeConversation.tsx)
                   ↓
              components/ (TextInput, MessageResponse, Markdown, ...)
                   ↓
              bridge/ (会话桥接、远程通信、JWT、repl bridge)
                   ↓
              tools/ + tools.ts  (getAllBaseTools, getTools, assembleToolPool)
                   ↓
              utils/ (shell, permissions, bash, git, skills, ...)
```

## Agent Loop

Claude Code 使用 **LangGraph** StateGraph（和 navi 相同），核心在 `screens/REPL.tsx`：
- llm_node ↔ tool_node 循环
- 通过 `bridge/bridgeMessaging.ts` 处理消息流
- 支持 plan mode (enter/exit plan mode tools)
- 支持 coordinator mode（多 Agent 协调）

关键设计决策：
- 所有工具统一通过 `tools.ts:getTools()` 获取，按 mode 过滤
- `assembleToolPool()` 合并内置工具 + MCP 工具，去重
- 简单模式（`CLAUDE_CODE_SIMPLE`）只用 Bash + Read + Edit 三个工具

## 工具系统

**核心文件**: `tools.ts` (387 行)

工具注册链路：
```
tools.ts:getAllBaseTools()  →  返回完整工具列表（静态 import + feature flag 条件）
     ↓
tools.ts:getTools(permissionContext)  →  按 mode 过滤 + deny rules 过滤
     ↓
tools.ts:assembleToolPool()  →  合并内置 + MCP 工具，排序去重
```

**21 个基础工具**（始终启用）:
- AgentTool, BashTool, FileReadTool, FileEditTool, FileWriteTool
- GlobTool, GrepTool, WebFetchTool, WebSearchTool, TodoWriteTool
- TaskOutputTool, TaskStopTool, AskUserQuestionTool, SkillTool
- EnterPlanModeTool, ExitPlanModeV2Tool, NotebookEditTool
- SendMessageTool, BriefTool, ListMcpResourcesTool, ReadMcpResourceTool

**33+ 个条件工具**（feature flag / env 控制）:
- PowerShellTool, TeamCreateTool, TeamDeleteTool, WorkflowTool
- CronCreateTool/DeleteTool/ListTool, SleepTool, LSPTool, ConfigTool 等

**工具分类与权限**:
- `ALL_AGENT_DISALLOWED_TOOLS` — 子 Agent 禁止使用的工具
- `ASYNC_AGENT_ALLOWED_TOOLS` — 异步 Agent 允许的工具白名单
- `COORDINATOR_MODE_ALLOWED_TOOLS` — coordinator 专用工具
- `IN_PROCESS_TEAMMATE_ALLOWED_TOOLS` — 进程内 teammate 额外工具

## 审批系统

三层结构（`utils/permissions/`）：
1. **permissions.ts** — 规则匹配引擎，支持 deny/allow 规则
2. **classifierApprovals.ts** — 分类器自动审批（根据工具类型+参数判断风险）
3. Shell 工具内联审批 — BashTool/PowerShellTool 自身处理审批

## 会话管理

- **本地模式**: `~/.claude/sessions/` 下按 session ID 存储 JSON
- **远程模式**: bridge 机制 (`bridge/`) — 通过 WebSocket/HTTP 与 claude.ai 通信
- session ID 兼容层: `cse_*` ↔ `session_*` 格式转换

## UI 架构

基于 **Ink**（React for terminal）:
```
ink/ 目录 — Ink 框架本身
  ├── renderer.ts, reconciler.ts  — React reconciler
  ├── components/                  — Box, Text, etc.
  ├── hooks/                       — useInput, useStdin, etc.
  └── dom.ts, output.ts            — 终端渲染底层

components/ 目录 — Claude Code 业务组件
  ├── App.tsx, TextInput.tsx, MessageResponse.tsx
  ├── Markdown.tsx, CompactSummary.tsx
  └── Settings/, messages/, tasks/
```

## 技能系统

- 从项目 `_navi/skills/` 和 `~/.navi/skills/` 加载
- `SkillTool` 调用后，SKILL.md 内容注入系统提示
- `utils/skills/` 处理技能加载和格式化

## 对 navi 有价值的模式

1. **工具注册**: `getAllBaseTools()` 集中管理 + feature flag 条件编译，比 navi 手动 `_register_tools()` 更清晰
2. **工具权限分层**: 主 Agent / 异步 Agent / teammate / coordinator 各有不同工具集
3. **简单模式**: CLI 参数 `--simple` 让 Agent 只用 Bash+Read+Edit，减少模型选择困难
4. **sendMessageTool**: Agent 间通过消息通道通信，不共享状态
5. **plan mode**: 用 EnterPlanModeTool/ExitPlanModeTool 切换模式，模型自主决定何时进入计划
