---
name: cli-tool-output-arch
description: CLI agent 工具输出架构设计与调试。当排查工具输出重复、输出丢失、输出乱序、实时流与结果渲染冲突、输出截断策略等问题时使用。也适用于为新的 CLI agent 工具设计输出通道。触发词：工具输出重复、output twice、双份输出、流式输出、tool result rendering、输出截断、truncat。
---

# CLI Agent 工具输出架构

## 核心问题

CLI agent 的工具执行涉及两层输出通道，设计不当会导致重复或冲突：

| 通道 | 时机 | 用途 | 典型实现 |
|------|------|------|----------|
| **实时流** | 执行过程中 | 让用户看到命令正在跑 | `on_output` callback, stdout tail |
| **结果渲染** | 执行完成后 | 结构化展示元信息 | event_handler 处理 `tool_result` 事件 |

**反模式**：两层都输出同一份内容（如命令的 stdout），用户看到双份。

## 三种设计方案

### 方案 A：只有结果渲染（Hermes 方式）

工具内部不输出任何内容，只返回 JSON。event_handler 在工具完成后统一渲染一行摘要。

```
tool.execute() → {output, exit_code} → event_handler(tool_result) → "Used terminal (ls -la) · 0.3s"
```

Hermes 的 `_on_tool_progress()` 实现（`cli.py:10529`）：
- `tool.started`：只更新 spinner 文本（`💻 ls -la`），不输出任何内容
- `tool.completed`：打印一行滚动历史（`get_cute_tool_message()`），可配置模式（all/new/off）

- 优点：简单，无重复风险
- 缺点：长命令无实时反馈，用户以为卡死

### 方案 B：实时流 + 精简结果（Navi 采用）

工具通过 `on_output` 实时输出原始 stdout，event_handler 只渲染元信息（exit_code、耗时），不重复输出 stdout。

```
tool.execute() → on_output(stdout) [实时] + return {exit_code, ...}
                                    ↓
event_handler(tool_result) → 只显示 "exit_code=0  2.3s" [不重复 stdout]
```

- 优点：实时反馈 + 无重复
- 缺点：需要明确定义两层的职责边界

### 方案 C：实时流 + 结果渲染但去重

两层都输出，但结果渲染时检查是否已通过实时流输出过，跳过重复部分。

- 优点：兼容性好
- 缺点：状态管理复杂，容易出错

## Navi 的修复（已完成）

**问题**：`RunCommandTool` 通过 `on_output` 实时输出 stdout，`print_agent_event` 又输出 `tool_result.output`，导致双份。

**修复**：`print_agent_event` 中命令工具的处理只显示元信息：

```python
# main.py print_agent_event
elif tool_name in {"bash", "powershell"}:
    exit_code = tool_result.get("exit_code")
    if tool_result.get("ok") or exit_code == 0:
        _p(f"[green]┊ exit_code=0[/green]{elapsed_str}")
    else:
        _p(f"[red]┊ exit_code={exit_code}[/red]{elapsed_str}")
    # 不再输出 output — 已由 on_output 实时输出
```

## 输出截断机制

**关键区分**：截断发生在三个不同层面，不要混淆：

| 层面 | 时机 | 影响对象 | 目的 |
|------|------|----------|------|
| **实时显示** | 执行过程中 | 用户看到的终端输出 | 让用户实时了解执行情况 |
| **返回值截断** | 执行完成后 | 模型看到的 `output` 字段 | 避免过长输出占用上下文窗口 |
| **UI 摘要** | 执行完成后 | 用户看到的结果摘要 | 提供元信息（exit_code、耗时） |

### Navi 的截断策略

| 层面 | 截断行为 | 阈值 |
|------|----------|------|
| **实时显示** | 通过 `on_output` 逐行输出到 CLI，**不截断** | 无 |
| **返回值** | 工具返回给模型的 `output` 字段超过阈值时截断 | `max_output_chars` = 50,000 字符 |
| **UI 摘要** | `print_agent_event` 对命令工具只显示 exit_code | 不显示输出内容 |

**截断实现**（`builtin.py` RunCommandTool）：

```python
def _truncate_output(self, text: str) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= self.max_output_chars:
        return text, False
    truncated_text = text[:self.max_output_chars] + "\n\n... 输出已截断 ..."
    return truncated_text, True
```

**关键点**：用户在 CLI 看到的是完整输出（实时流），截断只影响返回给模型的 `output` 字段。

### Hermes 的截断策略

**⚠️ 常见混淆点**：Hermes 的 40%/60% 头尾保留策略是针对**返回值**的，不是 CLI 实时显示。实时显示是逐行输出的，无法预知总长度来做头尾保留。

Hermes 的截断发生在**命令执行完成后**，不是实时显示时。

#### 1. 返回值截断（`acp_adapter/tools.py`）

按工具类型分级截断，用于格式化返回给模型的内容：

```python
def _truncate_text(text: str, limit: int = 5000) -> str:
    if len(text) <= limit:
        return text
    return text[:max(0, limit - 100)] + f"\n... ({len(text)} chars total, truncated)"
```

| 工具/场景 | limit | 代码位置 |
|-----------|-------|----------|
| 通用默认 | 5,000 | `_truncate_text()` 默认参数 |
| terminal output | 5,000 | `_format_process_result` |
| terminal error | 2,000 | `_format_process_result` |
| terminal 总结果 | 7,000 | `_format_process_result` |
| search_files | 7,000 | `_format_search_files_result` |
| web_extract | 7,000 | `_format_web_extract_result` |
| session_search | 8,000 | `_format_session_search_result` |

#### 2. CLI 界面截断（`tools/terminal_tool.py`）

Hermes terminal 工具在**命令执行完成后**对输出进行截断（第 2131-2141 行）：

```python
from tools.tool_output_limits import get_max_bytes
MAX_OUTPUT_CHARS = get_max_bytes()  # 默认 50,000
if len(output) > MAX_OUTPUT_CHARS:
    head_chars = int(MAX_OUTPUT_CHARS * 0.4)   # 40% 头部
    tail_chars = MAX_OUTPUT_CHARS - head_chars  # 60% 尾部
    omitted = len(output) - head_chars - tail_chars
    truncated_notice = (
        f"\n\n... [OUTPUT TRUNCATED - {omitted} chars omitted "
        f"out of {len(output)} total] ...\n\n"
    )
    output = output[:head_chars] + truncated_notice + output[-tail_chars:]
```

**头尾保留策略**：
- 保留 **40% 头部**：错误信息、编译错误通常在开头
- 保留 **60% 尾部**：最新输出、测试结果通常在末尾
- 截断标记包含原始长度，方便判断

**重要澄清**：这个截断是在 `env.execute()` 返回完整输出后执行的，不是实时显示时的截断。实时显示是逐行输出的，无法预知总长度来做头尾保留。

**可配置**：通过 `config.yaml` 的 `tool_output.max_bytes` 调整阈值。

### 设计建议

1. **实时流不截断**：用户需要看到完整输出来判断命令执行情况
2. **返回值截断**：避免过长的输出占用模型上下文窗口
3. **CLI 界面截断用头尾保留策略**：比单纯保留头部更合理，错误在前、最新输出在后
4. **按工具类型设不同 limit**：终端输出、搜索结果、错误信息的重要性不同
5. **截断标记要明显**：包含原始长度信息，方便模型判断是否需要重跑

## 调试方法：沿数据流 grep

排查输出问题时，沿数据流逐层追踪：

```
1. 工具层：grep on_output / self.on_output  → 找到实时输出点
2. 事件层：grep tool_result / event_handler → 找到结果渲染点
3. UI层：grep _print / console.print        → 找到最终输出点
4. 检查哪些层输出了相同内容
```

关键变量名约定（Navi）：
- `on_output` — 工具实时输出回调
- `event_handler` / `print_agent_event` — 事件处理函数
- `stream_box` / `NaviStreamView` — UI 渲染组件
- `_truncate_output` / `max_output_chars` — 截断相关

关键变量名约定（Hermes）：
- `_truncate_text` / `limit` — 返回值截断
- `tool_output_limits.get_max_bytes()` — CLI 界面截断阈值
- `_on_tool_progress` — 工具进度回调

## 设计检查清单

添加新工具时，确认：

- [ ] 工具是否需要实时输出？（长命令 vs 瞬时操作）
- [ ] `on_output` 和 `event_handler` 输出的内容是否有重叠？
- [ ] 结果渲染是否只包含 `on_output` 未覆盖的元信息？
- [ ] 错误输出是否只在一个地方显示？
- [ ] 输出截断阈值是否合理？（参考同类工具的 limit）
- [ ] 截断后是否保留了关键信息？（考虑头尾保留策略）
