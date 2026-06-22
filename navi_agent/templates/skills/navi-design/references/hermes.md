# Hermes Agent 架构参考

> 源码路径: `E:\light_agent\hermes-agent\` | 语言: Python | 架构: 同步主循环 + 多线程

## 整体架构

```
cli.py (HermesCLI, ~700k 行超大型文件)
  ↓
run_agent.py (AIAgent, ~12k 行核心循环)
  ↓
  ├── model_tools.py  — 工具编排层
  ├── tools/  — 工具实现（自注册模式）
  │   ├── registry.py  — 注册表（AST 扫描发现）
  │   ├── file_operations.py, file_tools.py  — 文件操作
  │   ├── code_execution_tool.py  — 代码执行
  │   ├── browser_tool.py  — 浏览器自动化 (~165k)
  │   ├── delegate_tool.py  — 任务委派 (~122k)
  │   ├── mcp_tool.py  — MCP 集成 (~156k)
  │   ├── cronjob_tools.py  — 定时任务
  │   ├── environments/  — 终端后端（local, docker, ssh, modal, ...）
  │   └── ... 50+ 工具文件
  ├── toolsets.py  — 工具集定义
  ├── hermes_state.py  — SessionDB (SQLite FTS5)
  └── plugins/  — 插件系统
```

## Agent Loop (核心设计)

`AIAgent` (run_agent.py) 是核心：
- **同步主循环**: 不使用 asyncio 作为主循环，而是 `while True` + `threading`
- **工具执行**: 通过 persistent event loop 执行异步工具（`_get_tool_loop()` 复用 event loop，避免 "Event loop is closed" 错误）
- **流式处理**: `stream=True` 实时输出
- **多 provider 支持**: OpenAI、Anthropic、Google、OpenRouter 等通过 `plugins/model-providers/` 扩展

关键设计：
```python
# AIAgent.run_conversation() 简化版
while True:
    response = client.chat.completions.create(
        messages=messages,
        tools=openai_tools,
        stream=True,
    )
    # 处理 streaming chunks
    if tool_calls:
        for tc in tool_calls:
            result = handle_function_call(tc.name, tc.args)
            messages.append(tool_result)
    else:
        break  # 无工具调用，返回文本
```

## 工具系统（自注册模式）

**这是三个项目中最独特的工具系统。**

```
tools/registry.py  (无依赖，被所有工具文件 import)
     ↑
tools/*.py  (每个文件调用 registry.register() 自注册)
     ↑
model_tools.py  (触发 discover_builtin_tools()，扫描 tools/ 目录)
     ↑
run_agent.py, cli.py, batch_runner.py  (消费者)
```

**注册模式**:
```python
# tools/file_tools.py
from tools.registry import registry

registry.register(
    name="read_file",
    description="Read a file from the local filesystem...",
    parameters={...},  # JSON Schema
    handler=read_file_handler,  # callable
    toolset="core",  # 所属工具集
    check_fn=lambda: True,  # 可用性检查
)
```

**工具集系统** (`toolsets.py`):
- `_HERMES_CORE_TOOLS`: 50+ 核心工具名称列表
- 平台专用工具集：telegram, discord, slack 等
- webhook 安全工具集：限制第三方触发时的工具范围
- 工具集可组合：`resolve_toolset("full_stack")` 递归展开

**发现机制**:
- `discover_builtin_tools()` 遍历 `tools/*.py`
- 使用 AST 解析检测 `registry.register(...)` 调用（不实际 import）
- 按需 import 和注册

## 审批系统

`tools/approval.py` (~64k 行) — 极为详尽的审批系统：
- 多 source 审批路由（CLI、gateway、platform）
- 审批请求可通过不同 UI 层展示
- 支持 remember 选项（`approve_for_session`）

## 会话管理

`hermes_state.py` (~145k 行) — SQLite 存储：
- **SessionDB**: 基于 SQLite + FTS5 全文搜索
- 消息历史、工具调用结果、对话摘要
- `hermes sessions` CLI 命令浏览和恢复会话

## 插件系统

`plugins/` 目录，插件即 Python 包：
```
plugins/
├── memory/          — 记忆提供者（honcho, mem0, supermemory）
├── context_engine/  — 上下文引擎
├── model-providers/ — 模型后端（openrouter, anthropic, gmi）
├── kanban/          — 多 Agent 看板调度
├── image_gen/       — 图像生成
├── observability/   — 指标/追踪/日志
└── ...
```

## 多平台 Gateway

`gateway/` — 消息网关，支持 20+ 平台：
- Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Mattermost
- 微信, 企业微信, 飞书, QQ bot, 钉钉
- Email, SMS, Webhook, API Server
- 每个平台在 `gateway/platforms/` 有自己的 adapter

## 终端环境

`tools/environments/` — 支持多种代码执行后端：
- Local (subprocess)
- Docker (container)
- SSH (remote)
- Modal (serverless)
- Daytona (cloud dev env)
- Singularity (HPC)

## 对 navi 有价值的模式

1. **自注册工具系统**: `registry.register()` 比 navi 的手动注册更解耦。添加新工具只需新建文件，不需要修改运行时
2. **工具集分组**: 按场景（core/webhook）或平台（telegram/slack）组织工具，按需激活
3. **AST 扫描发现**: 不 import 即可发现工具模块，避免启动时的导入开销
4. **Persistent Event Loop**: 复用 asyncio event loop 避免 "Event loop is closed" 错误
5. **多环境支持**: 终端后端可插拔（local/docker/ssh），navi 也可受益
6. **SQLite FTS5**: 全文搜索会话历史，比 JSON 文件搜索更快
7. **插件系统**: 清晰的插件目录结构，memory/model-providers/kanban 等可扩展点
8. **Gateway 多平台**: 虽然 navi 不需要 20+ 平台，但 gateway 的 adapter 模式值得参考
9. **Checkpoint 管理**: `checkpoint_manager.py` 提供对话历史快照和回滚
10. **Browser 工具**: `browser_tool.py` (165k) 是基于 CDP 的完整浏览器自动化实现
