---
name: mermaid
description: "Create Mermaid diagrams (flowchart, mindmap, sequence, class, etc.) as HTML files. Use when the user asks for a diagram, flowchart, mind map, sequence diagram, architecture diagram, or any visual chart. Covers syntax rules, common pitfalls, and delivery workflow."
---

# Mermaid Diagram Skill

Generate Mermaid diagrams as standalone HTML files that open directly in a browser.

## Workflow

1. Write Mermaid code following the syntax rules below
2. Save as `.html` file using the HTML template
3. Send the file path to the user (double-click opens in browser)

## HTML Template

```html
<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="UTF-8">
<title>{TITLE}</title>
<style>
  body {
    display: flex; justify-content: center; align-items: center;
    min-height: 100vh; margin: 0; background: #f8f9fa; font-family: sans-serif;
  }
  .mermaid {
    background: white; padding: 40px; border-radius: 12px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.08);
  }
</style>
</head>
<body>
<div class="mermaid">
{MERMAID_CODE}
</div>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
<script>mermaid.initialize({startOnLoad:true, theme:'default'});</script>
</body>
</html>
```

## Diagram Types

| Type | Syntax | Use case |
|------|--------|----------|
| flowchart | `flowchart TD` / `flowchart LR` | 流程图、架构图 |
| mindmap | `mindmap` | 思维导图 |
| sequence | `sequenceDiagram` | 时序图 |
| class | `classDiagram` | 类图 |
| state | `stateDiagram-v2` | 状态机 |
| er | `erDiagram` | ER图 |
| gantt | `gantt` | 甘特图 |
| pie | `pie` | 饼图 |

## Syntax Rules (CRITICAL)

### General
- Use `TD` (top-down) or `LR` (left-right) for flowchart direction
- Node IDs must be alphanumeric (no special chars)
- Use `["text"]` for node labels with special characters
- Quotes inside labels: use `#quot;` not `"`

### Mindmap (most error-prone)

**Indentation**: Exactly 2 spaces per level. No tabs.

**Root node**: `root((text))` — circle shape

**Node text rules**:
- NO `**bold**` or `*italic*` — not supported
- NO `<br>` — not supported
- NO `→`, `×`, `·` — use `->`, `x`, `*` instead
- NO `#`, `{`, `}`, `[`, `]` in bare text — wrap in quotes if needed
- Keep text simple and short

**Valid mindmap**:
```
mindmap
  root((主题))
    分支A
      叶子1
      叶子2
    分支B
      叶子3
```

**INVALID** (will cause "Syntax error in text"):
```
mindmap
  root((主题<br>副标题))
    **分支A**
      叶子1: V → V
```

### Flowchart

**Node shapes**:
- `[text]` — rectangle
- `(text)` — rounded
- `{text}` — diamond
- `((text))` — circle
- `>text]` — flag
- `[/text/]` — parallelogram

**Connections**:
- `A --> B` — arrow
- `A --- B` — line (no arrow)
- `A -->|text| B` — labeled arrow
- `A -.-> B` — dotted arrow
- `A ==> B` — thick arrow

**Subgraphs**:
```
subgraph title
  A --> B
end
```

### Sequence Diagram

```
sequenceDiagram
  participant A as Alice
  participant B as Bob
  A->>B: Hello
  B-->>A: Hi back
  A->>B: How are you?
  B-->>A: Great!
```

Arrows: `->>` solid, `-->>` dashed, `-x` solid cross, `--x` dashed cross

## Common Pitfalls

1. **"Syntax error in text"**: Usually caused by special chars in node text or `**bold**` in mindmap. Simplify text.
2. **Indentation errors**: Mindmap requires exactly 2-space indent per level. No mixed tabs/spaces.
3. **Chinese characters**: Work fine, but avoid mixing with special punctuation in node text.
4. **Long text in nodes**: Keep concise. Use `["long text with spaces"]` in flowcharts.
5. **Mermaid version**: Use CDN `mermaid@11` or later for best compatibility.

## Delivery

- Always save as `.html` file (standalone, no server needed)
- Tell the user the file path — they double-click to open
- If the user wants a shareable link, paste the mermaid code into [mermaid.live](https://mermaid.live) and share the URL
