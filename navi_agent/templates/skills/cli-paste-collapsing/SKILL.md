---
name: cli-paste-collapsing
description: CLI agent 大段粘贴处理模式。当用户在输入框粘贴大量文本（代码、日志、配置等）时，将内容折叠为文件引用以保持输入框清晰，提交时再展开为原始内容。适用于：实现 CLI agent 输入框、处理用户粘贴、优化大段文本输入体验。触发词：粘贴折叠、paste collapsing、大段输入、输入框优化、paste handling。
---

# CLI Paste Collapsing

大段粘贴折叠是 CLI agent 的输入优化模式：检测用户粘贴的大量文本，保存到临时文件，在输入框显示精简占位符，提交时展开为原始内容。

## 核心流程

```
用户粘贴 → 检测是否"大段" → 是 → 保存文件 + 显示占位符
                            → 否 → 直接插入输入框
用户提交 → 展开占位符为原始内容 → 发送给 agent
```

## 检测阈值

两个阈值，任一触发即折叠：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `paste_collapse_threshold` | 5 | 行数阈值 |
| `paste_collapse_char_threshold` | 2000 | 字符数阈值 |

斜杠命令（`/` 开头）不触发折叠，避免大参数被错误折叠。

## 两种实现路径

### Path A: Bracketed Paste（推荐）

终端支持 bracketed paste 时，`Ctrl+V`/`Cmd+V` 触发 `BracketedPaste` 事件，直接在事件处理器中折叠：

```python
def handle_paste(event):
    pasted_text = event.data
    line_count = pasted_text.count('\n')
    
    if (line_count >= threshold or len(pasted_text) >= char_threshold):
        # 保存到文件
        paste_file = paste_dir / f"paste_{counter}_{timestamp}.txt"
        paste_file.write_text(pasted_text)
        # 插入占位符
        placeholder = f"[Pasted text #{counter}: {line_count + 1} lines → {paste_file}]"
        buffer.insert_text(placeholder)
    else:
        buffer.insert_text(pasted_text)
```

### Path B: Text Change 回退

不支持 bracketed paste 的终端，通过 `on_text_changed` 事件检测：

```python
def on_text_changed(buffer):
    chars_added = len(text) - prev_text_len
    newlines_added = line_count - prev_newline_count
    
    # 检测是否为粘贴操作
    is_paste = chars_added > 1 or newlines_added >= 4
    
    if is_paste and (lines_hit or chars_hit):
        # 折叠逻辑同 Path A
```

**关键**：`Alt+Enter` 只添加 1 个换行，不会触发 `newlines_added >= 4` 的误判。

## 占位符格式

### CLI 模式（prompt_toolkit）

```
[Pasted text #1: 15 lines → ~/.hermes/pastes/paste_1_143022.txt]
```

- 提交时用正则展开：`\[Pasted text #\d+: \d+ lines → (.+?)\]`
- 支持多个占位符共存

### TUI 模式（React/Ink）

```
[[ first 16 chars.. [15 lines] .. last 28 chars ]]
```

- 使用 `edgePreview()` 生成首尾预览
- 存储在 `pasteSnips` 状态中，提交时用 `expandSnips()` 展开
- 限制：最多 32 个片段，总计 4MB

## 辅助处理

### 1. 清理泄漏的 Bracketed Paste 标记

终端解析失败时，标记可能泄漏为可见文本：

```python
def strip_leaked_bracketed_paste_wrappers(text):
    text = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
    text = text.replace("^[[200~", "").replace("^[[201~", "")
    # 处理降级形式
    text = re.sub(r'(^|[\s\n>:\]\)])\[200~', r'\1', text)
    text = re.sub(r'\[201~(?=$|[\s\n<\[\(\):;.,!?])', '', text)
    return text
```

### 2. Bracketed Paste 超时恢复

patch `Vt100Parser.feed()`，当 end mark `ESC[201~` 丢失时，2 秒后自动 flush：

```python
_BP_TIMEOUT_S = 2.0

if now - bp_start > _BP_TIMEOUT_S:
    # 超时，flush 缓冲内容
    paste_content = self_parser._paste_buffer
    self_parser.feed_key_callback(KeyPress(Keys.BracketedPaste, paste_content))
```

### 3. Surrogate 字符清理

Word/Google Docs 粘贴可能包含无效 surrogate：

```python
from run_agent import _sanitize_surrogates
pasted_text = _sanitize_surrogates(pasted_text)
```

### 4. 文件路径检测

粘贴的文件路径应走文件拖放逻辑，不触发折叠：

```python
def detect_file_drop(user_input):
    # 检测是否为真实文件路径
    # 支持引号包裹、转义空格、Termux 路径
    ...
```

## 文件存储

- 位置：`~/.hermes/pastes/`（或 `~/.navi/pastes/`）
- 命名：`paste_{counter}_{HHMMSS}.txt`
- 清理：定期扫描删除过期文件

## 性能监控

粘贴处理阻塞 prompt_toolkit 事件循环，需监控耗时：

```python
start = time.perf_counter()
# ... 处理逻辑 ...
elapsed_ms = (time.perf_counter() - start) * 1000
if elapsed_ms > 500:
    logger.warning(f"Slow bracketed-paste handler: {elapsed_ms:.1f}ms")
```

## 配置项

```yaml
# CLI 模式
paste_collapse_threshold: 5          # 行数阈值
paste_collapse_threshold_fallback: 5 # 回退模式行数阈值
paste_collapse_char_threshold: 2000  # 字符数阈值

# TUI 模式
pasteCollapseLines: N                # 从 gateway 获取
pasteCollapseChars: N                # 从 gateway 获取
```

## 参考实现

- **Hermes CLI**: `hermes_agent/cli.py` L13350-13570
- **Hermes TUI**: `hermes_agent/ui-tui/src/app/useComposerState.ts`
