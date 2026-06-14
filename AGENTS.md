# AGENTS.md


**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

## 5. 文件结构

- `navi_agent/cli/main.py`: Typer 命令入口。当前仍保留部分渲染、slash command、审批 fallback helper，尚未完全瘦身成纯入口。
- `navi_agent/cli/chat_controller.py`: 交互式 CLI 的编排层，连接 prompt UI、runtime、审批、状态提示和一轮对话生命周期。
- `navi_agent/cli/prompt_ui.py`: prompt_toolkit 输入层，只负责输入框、按键、模型选择、审批选择等 UI 本地状态；`Ctrl+C` 统一走全局 handler，再按 running/approval/idle 状态分发；不要直接操作 runtime。
- `navi_agent/cli/stream_box.py`: 流式输出框。
- `navi_agent/cli/markdown_tables.py`: stream_box 的 Markdown 表格渲染辅助。
- `navi_agent/tools/approval_broker.py`: runtime 线程等待审批与 UI 线程响应审批之间的桥。
- `navi_agent/runtime/agent.py`: agent 主执行逻辑，负责模型调用、工具调度、审批检查、历史写入；每轮通过 `TurnScope` 管理中断状态。
- `navi_agent/runtime/interrupt_scope.py`: 一轮对话的中断作用域，集中管理 `cancel_event`、执行线程、工具 worker、模型 aborter、审批 canceller。
- `navi_agent/runtime/interruptible.py`: 阻塞操作的统一入口，当前包含 `run_model_stream()`、`wait_approval()`、`tool_worker()`。
- `navi_agent/model/request.py`: 模型流式请求 worker。runtime 控制线程轮询 chunk 和 cancel，必要时 abort 当前请求。
- `navi_agent/model/router.py`: 模型 provider/router。普通调用保留共享 client，交互中断路径使用 request-local client。
- `navi_agent/tools/builtin.py`: 内置工具实现；`RunCommandTool` 需要能在中断时杀掉 subprocess；`SearchSessionTool` 支持 DISCOVERY/SCROLL/BROWSE 三种模式。
- `navi_agent/tools/registry.py`: 工具注册表。
- `navi_agent/storage/history_store.py`: SQLite 会话历史存储。FTS5 全文搜索（unicode61 + trigram 双表，触发器自动同步）。关键方法：`search_messages()`、`get_messages_around()`、`get_anchored_view()`（window + bookends）、`get_session()`、`list_sessions_rich()`。
- `navi_agent/storage/memory_store.py`: 长期记忆存储。
- `navi_agent/storage/agent_store.py`: 子 agent 实例存储。
- `navi_agent/context/context_manager.py`: 运行上下文组装。
- `navi_agent/context/compressor.py`: 上下文压缩。
- `navi_agent/integrations/mcp_client.py` / `navi_agent/integrations/mcp_commands.py`: MCP 集成。
- `navi_agent/skills/skill_manage.py`: skill 文件管理工具。

## 6. 当前项目思路

- CLI 分层目标：UI 和编排用 async；阻塞 runtime、工具执行用受控线程；外部命令用可杀的 subprocess。
- UI 主线程只处理输入、按键和渲染状态；runtime 在线程里跑；二者只通过回调、future、事件或 broker 通信。
- `Ctrl+C` 语义必须区分 `Cancel` 和 `Reject`：审批态 `Ctrl+C` 是取消本轮，不是拒绝工具；`Reject` 只表示用户拒绝当前工具，模型可以继续。
- 运行态 `Ctrl+C` 必须稳定进入 `prompt_ui._handle_ctrl_c()`，再调用 `on_cancel`；如果看不到 `Interrupt requested...`，优先检查 UI 按键入口，而不是 runtime abort。
- 运行态第二次 `Ctrl+C` 只标记 `force_exit`，不要提前 `event.app.exit()`；否则 prompt_toolkit 布局先退出，runtime 未收尾时输出会跑到输入框下方。
- 每轮 `run_turn()` 创建一个 `TurnScope`；`runtime.interrupt()` 只取消当前 scope，由 scope 统一设置 cancel、线程级 interrupt，并调用已注册的 abort/cancel 回调。
- 模型流式请求不能直接卡住 runtime 控制层。当前实现是 request worker 执行阻塞 API，runtime 控制层轮询并在中断时关闭当前 stream/client。
- 阻塞路径应通过 `interruptible.py` 的 wrapper 进入：模型走 `run_model_stream()`，审批走 `wait_approval()`，工具 worker 进入 `tool_worker()`。
- 中断保存历史的原则：本轮 user 会写入会话；未完成的 assistant 文本不落库；已写入的 assistant tool_calls 必须补齐中断 tool message，避免 resume 时历史结构坏掉。
- 优先模仿 Hermes 的中断思路：设置 cancel 标记、设置线程级 cooperative interrupt、abort 当前请求/进程，让执行层自行抛出并清理，不强杀 Python 线程。
- 会话搜索（`SearchSessionTool`）三种模式复刻 Hermes：DISCOVERY（FTS5 搜索 + lineage 去重 + bookends）、SCROLL（锚定消息窗口）、BROWSE（最近会话列表）。`parent_session_id` 链用于压缩会话的 lineage 归并。搜索结果自动跳过当前活跃会话。

## 7. 未完成任务

- 第三阶段尚未做：`RuntimeEvent` / `UiEvent` 类型、`terminal_renderer.py`、统一状态栏数据模型。
- 工具系统尚未全面 context 化：还没有 `ToolExecutionContext(scope)`，所以 `RunCommandTool` 仍通过现有 `is_interrupted()` 路径杀 subprocess。
- `cli/main.py` 尚未完全变成“只保留命令入口和启动 controller”；仍有部分历史 helper 留在里面。
