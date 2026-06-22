---
name: cli-input-paste-handling
description: CLI agent 大文本粘贴处理机制。当用户询问"粘贴大量文本怎么处理"、"输入框粘贴"、"paste handling"、"large text input"、"多行输入折叠"、"bracketed paste"时使用。也适用于实现粘贴折叠、引用展开、终端兼容性处理等功能。触发词：粘贴、paste、大文本、折叠、collapse、bracketed paste、终端兼容。
---

# CLI Agent 大文本粘贴处理

## 核心问题

用户粘贴大量文本到 CLI 输入框时，需要解决：
1. **UI 卡顿**：大量文本直接插入输入框会导致 prompt_toolkit 渲染卡顿
2. **上下文膨胀**：超长粘贴直接发送给模型会浪费 token
3. **终端兼容性**：不同终端对 bracketed paste 的支持程度不同

## Hermes 的粘贴折叠机制

### 两种触发路径

| 路径 | 触发条件 | 实现位置 |
|------|---------|---------|
| **Bracketed Paste** | 终端支持 `\x1b[200~` ... `\x1b[201~` | `handle_paste` 事件处理器 |
| **Fallback 检测** | 不支持 bracketed paste 的终端 | `_on_text_changed` 回调 |

### 折叠阈值（可配置）

```python
# config.yaml
paste_collapse_threshold: 5           # 行数阈值
paste_collapse_char_threshold: 2000   # 字符数阈值
paste_collapse_threshold_fallback: 5  # Fallback 模式的行数阈值
```

**触发条件**（满足任一即折叠）：
- `line_count >= threshold`（默认 5 行）
- `len(text) >= char_threshold`（默认 2000 字符）
- 且输入不是斜杠命令（`not buf.text.strip().startswith('/')`）

### Bracketed Paste 处理流程

```
用户粘贴
  ↓
handle_paste(event)
  ├─ 规范化换行：\r\n → \n, \r → \n
  ├─ 清理泄漏标记：_strip_leaked_bracketed_paste_wrappers()
  ├─ 清理鼠标报告：_strip_leaked_terminal_responses_with_meta()
  ├─ 纯图片粘贴？→ _should_auto_attach_clipboard_image_on_paste()
  ├─ 清理 surrogate 字符
  └─ 判断是否折叠
       ├─ 超阈值 → 保存到文件，插入占位符
       └─ 未超阈值 → 直接插入 buf.insert_text()
```

### Fallback 检测逻辑

当终端不支持 bracketed paste 时，通过 `_on_text_changed` 检测：

```python
def _on_text_changed(buf):
    # 两种启发式（满足任一即判定为粘贴）：
    # 1. chars_added > 1 — 终端一次性交付所有字符
    # 2. newlines_added >= 4 — 终端逐字符交付但批量换行
    
    is_paste = chars_added > 1 or newlines_added >= 4
    
    if (lines_hit or chars_hit) and is_paste and not text.startswith('/'):
        # 折叠处理
```

### 折叠后的占位符格式

```
[Pasted text #1: 10 lines → C:\Users\29924\.hermes\pastes\paste_1_143052.txt]
```

**文件路径**：`~/.hermes/pastes/paste_{counter}_{HHMMSS}.txt`

## 提交时展开引用

用户提交输入时，`_expand_paste_references` 将占位符替换回完整内容：

```python
def _expand_paste_references(self, text: str | None) -> str:
    """Expand [Pasted text #N -> file] placeholders into file contents."""
    paste_ref_re = re.compile(r'\[Pasted text #\d+: \d+ lines \u2192 (.+?)\]')
    
    def _expand_ref(match):
        path = Path(match.group(1))
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, IOError):
            return match.group(0)  # 文件丢失则保留占位符
    
    return paste_ref_re.sub(_expand_ref, text)
```

**调用位置**：
- CLI 提交时（`cli.py:14403`）
- 外部编辑器打开前（`cli.py:4590`）

## Bracketed Paste 超时补丁

prompt_toolkit 的 `Vt100Parser.feed()` 在等待 `\x1b[201~` 结束标记时可能永远卡住（终端 race、SSH 断连等）。Hermes 的补丁：

```python
def _apply_bracketed_paste_timeout_patch():
    _BP_TIMEOUT_S = 2.0  # 最多等 2 秒
    
    def _patched_vt100_feed(self_parser, data):
        if self_parser._in_bracketed_paste:
            # 超时后强制 flush 为 BracketedPaste 事件
            if now - bp_start > _BP_TIMEOUT_S:
                paste_content = self_parser._paste_buffer
                self_parser.feed_key_callback(
                    _PtKeyPress(_PtKeys.BracketedPaste, paste_content)
                )
```

## Bracketed Paste 标记清理

终端解析失败时，bracketed paste 标记可能泄漏为字面文本：

```python
def _strip_leaked_bracketed_paste_wrappers(text: str) -> str:
    text = text.replace("\x1b[200~", "").replace("\x1b[201~", "")
    text = text.replace("^[[200~", "").replace("^[[201~", "")
    # 处理降级形式
    text = re.sub(r"(^|[\s\n>:\]\)])\[200~", r"\1", text)
    text = re.sub(r"\[201~(?=$|[\s\n<\[\(\):;.,!?])", "", text)
    return text
```

## TUI 版本的处理（ui-tui）

TUI 使用 `PasteSnippet` 接口：

```typescript
interface PasteSnippet {
  label: string    // 如 "Pasted text #1"
  path?: string    // 可选的文件路径
  text: string     // 粘贴内容
}
```

**限制**：
- `PASTE_SNIP_MAX_COUNT = 32`（最多 32 个粘贴片段）
- `PASTE_SNIP_MAX_TOTAL_BYTES = 4 * 1024 * 1024`（总计 4MB）

**展开逻辑**：
```typescript
const expandSnips = (snips: PasteSnippet[]) => {
  const byLabel = new Map<string, string[]>()
  for (const { label, text } of snips) {
    const hit = byLabel.get(label)
    hit ? hit.push(text) : byLabel.set(label, [text])
  }
  return (value: string) => 
    value.replace(PASTE_SNIPPET_RE, tok => byLabel.get(tok)?.shift() ?? tok)
}
```

占位符格式：`[[Pasted text #1: 10 lines]]`

## 设计要点

1. **折叠而非丢弃**：保存到文件，提交时展开，模型看到完整内容
2. **双路径兼容**：bracketed paste + fallback 检测，覆盖所有终端
3. **斜杠命令豁免**：折叠逻辑跳过 `/command` 开头的输入
4. **超时保护**：防止终端丢弃结束标记导致永久卡死
5. **可配置阈值**：用户可根据使用习惯调整折叠阈值
