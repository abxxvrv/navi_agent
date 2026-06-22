---
name: sqlite-fts5-search
description: SQLite FTS5 全文搜索实现指南。当需要为 SQLite 数据库添加全文搜索功能时使用，包括：CJK（中日韩）子串搜索、倒排索引设计、触发器自动同步、查询清洗。适用于：会话历史搜索、文档检索、日志搜索、任何需要文本搜索的场景。触发词：FTS5、全文搜索、full text search、会话搜索、历史搜索、trigram、CJK 搜索。
---

# SQLite FTS5 全文搜索实现指南

## 核心架构：双表 + 触发器

```
messages 表 (主表)
    │
    ├───── INSERT/UPDATE/DELETE 触发器
    │
    ├──────────────────┬──────────────────┐
    ▼                  ▼                  
messages_fts         messages_fts_trigram
(unicode61 分词)      (trigram 分词)
拉丁语系搜索          CJK 子串搜索
```

**为什么需要两个 FTS5 表？**
- `unicode61`：拉丁语系分词好，但会把每个 CJK 字符拆成独立 token
- `trigram`：CJK 子串匹配好，但索引体积大、拉丁语系查询慢

## Step 1: 创建 FTS5 表

```sql
-- unicode61 FTS5（拉丁语系）
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tokenize='unicode61'
);

-- trigram FTS5（CJK 子串搜索）
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
    content,
    tokenize='trigram'       -- 关键：3 字节滑动窗口
);
```

### FTS5 表列设计原则

**只放需要搜索的列**。其他列（如 session_id、role）不要放，或用 UNINDEXED。

**UNINDEXED 列的代价**：
- 数据确实写进了 FTS5 表
- 但 FTS5 不会为它建立倒排索引
- 不能用列限定语法搜索（如 `session_id:xxx`）
- 只是"搭便车"存在那里，占空间但不参与搜索

**最佳实践**：FTS5 表只放 `content` 一列，其他需要搜索的字段在触发器中拼接到 content。

```sql
-- ✅ 推荐：只有一列
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tokenize='unicode61'
);

-- ❌ 不推荐：UNINDEXED 列增加存储开销
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    session_id UNINDEXED,
    role UNINDEXED,
    title UNINDEXED,
    tokenize='unicode61'
);
```

## Step 2: 触发器自动同步

**不要手动同步！** 用触发器保证 FTS5 表与主表一致。

### 索引内容选择

**关键决策**：触发器写入 FTS5 表的内容应该包含所有需要搜索的字段。

```sql
-- ✅ 推荐：拼接所有需要搜索的字段
INSERT INTO messages_fts(rowid, content)
VALUES (
    new.id,
    COALESCE(new.content, '') || ' ' ||
    COALESCE(new.tool_name, '') || ' ' ||
    COALESCE(new.tool_calls, '')
);

-- ❌ 不推荐：只索引 content_text，搜索不到 tool_name 和 tool_calls
INSERT INTO messages_fts(rowid, content)
VALUES (new.id, new.content_text);
```

**为什么拼接更好？**
- 搜索 `search_files` 能找到用过这个工具的消息
- 搜索 `docker` 能找到 tool_calls 参数中包含 docker 的消息
- 一次索引，多维度可搜

### 完整触发器实现

```sql
-- unicode61 表触发器
CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content)
    VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

-- trigram 表触发器（同样的模式）
CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts_trigram(rowid, content)
    VALUES (new.id, COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '') || ' ' || COALESCE(new.tool_calls, ''));
END;
-- ... UPDATE, DELETE 触发器同理
```

## Step 3: CJK 三路分发搜索

```python
def search_messages(self, query: str, limit: int = 20) -> list[dict]:
    """FTS5 全文搜索，自动选择最佳路径。"""
    
    # 1. 清洗查询
    query = self._sanitize_fts5_query(query)
    
    # 2. CJK 三路分发
    if self._contains_cjk(query):
        cjk_count = self._count_cjk(query)
        
        if cjk_count >= 3:
            # 路径 A: trigram FTS5（3+ CJK 字符）
            # 精确子串匹配，有索引加速
            return self._search_trigram(query, limit)
        else:
            # 路径 B: LIKE 回退（1-2 CJK 字符）
            # trigram 需要 ≥3 CJK 字符才能匹配
            return self._search_like(query, limit)
    else:
        # 路径 C: 标准 FTS5（拉丁语系）
        return self._search_standard(query, limit)
```

### Trigram 原理

```
文档: "大别山项目" (每个汉字 3 字节 UTF-8)

Trigram 分词（9 字节窗口，滑动 1 字节）:
  "大别山"  → [大₁大₂大₃别₁别₂别₃山₁山₂山₃]
  "别山项"  → [别₁别₂别₃山₁山₂山₃项₁项₂项₃]
  "山项目"  → [山₁山₂山₃项₁项₂项₃目₁目₂目₃]

查询: "别山" (6 字节 < 9 字节) → ❌ 无法匹配
查询: "别山项" (9 字节) → ✅ 精确子串匹配
```

**为什么 trigram 需要 ≥3 CJK 字符？**
- 每个 CJK 字符 = 3 字节 UTF-8
- trigram = 3 字节 = 1 个 CJK 字符（太短，无法匹配有意义的内容）
- 实际需要 3 个 CJK 字符 = 9 字节 = 1 个 trigram

### LIKE 回退（短 CJK 查询）

```python
def _search_like(self, query: str, limit: int) -> list[dict]:
    """短 CJK 查询回退到 LIKE（无索引，全表扫描）。"""
    non_op_tokens = [t for t in query.split() if t.upper() not in {"AND", "OR", "NOT"}]
    
    token_clauses = []
    like_params = []
    for tok in non_op_tokens:
        esc = tok.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        token_clauses.append("(m.content_text LIKE ? ESCAPE '\\')")
        like_params.append(f"%{esc}%")
    
    sql = f"""
        SELECT m.id, m.session_id, m.role, m.content_text, m.created_at
        FROM messages m
        WHERE {' OR '.join(token_clauses)}
        ORDER BY m.created_at DESC
        LIMIT ?
    """
    like_params.append(limit)
    return self._execute(sql, like_params)
```

## Step 4: 查询清洗（重要！）

FTS5 有自己的查询语法，用户输入可能包含特殊字符导致错误。

### ⚠️ 常见错误：过度引号化

**错误做法**：把所有含字母/数字的术语都包上引号

```python
# ❌ 错误：所有术语都引号化
sanitized = re.sub(r"[\w][\w\-\.]*[\w]", _quote_term, sanitized)
```

这会导致 `docker deployment` → `"docker" "deployment"`，变成**短语匹配**（要求相邻），而非默认的 **AND 匹配**（不要求相邻）。搜索召回率会显著下降。

**正确做法**：只引号化含 `-` / `.` / `_` 的术语

```python
# ✅ 正确：只引号化含分隔符的术语
sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
```

这样 `docker deployment` → `docker deployment`（保持 AND 语义），`my-app.config` → `"my-app.config"`（防止被分词器拆分）。

### 完整的查询清洗实现

```python
@staticmethod
def _sanitize_fts5_query(query: str) -> str:
    """清洗 FTS5 查询，处理特殊字符。"""
    if not query:
        return ""
    
    # 1. 保留引号包裹的短语
    quoted_phrases = []
    def _preserve_quoted(m):
        quoted_phrases.append(m.group(0))
        return f"__QUOTED_{len(quoted_phrases) - 1}__"
    sanitized = re.sub(r'"[^"]*"', _preserve_quoted, query)
    
    # 2. 移除 FTS5 特殊字符
    sanitized = re.sub(r'[+{}()\\"^]', " ", sanitized)
    
    # 3. 折叠重复的 *
    sanitized = re.sub(r"\*{2,}", "*", sanitized)
    
    # 4. 移除开头的 *（前缀搜索需要前面有字符）
    sanitized = re.sub(r"(^|\s)\*", r"\1", sanitized)
    
    # 5. 移除头部和尾部悬空布尔运算符
    sanitized = re.sub(r"(?i)^(AND|OR|NOT)\b\s*", "", sanitized.strip())
    sanitized = re.sub(r"(?i)\s+(AND|OR|NOT)\s*$", "", sanitized.strip())
    
    # 6. 只引号化含 - . _ 的术语（防止被分词器拆分）
    # 注意：不要引号化所有术语！否则会变成短语匹配
    sanitized = re.sub(r"\b(\w+(?:[._-]\w+)+)\b", r'"\1"', sanitized)
    
    # 7. 恢复引号短语
    for i, phrase in enumerate(quoted_phrases):
        sanitized = sanitized.replace(f"__QUOTED_{i}__", phrase)
    
    return sanitized.strip()
```

## Step 5: 上下文窗口（分层设计）

搜索结果分两层返回，避免一次加载太多数据：

```python
def search_messages(self, query: str, limit: int = 20) -> list[dict]:
    """第一层：返回匹配消息 + ±1 浅上下文。"""
    matches = self._execute_fts_query(query, limit)
    
    # 添加 ±1 上下文（浅预览）
    for match in matches:
        match["context"] = self._get_context_messages(match["id"], window=1)
    
    return matches

def get_messages_around(self, session_id: str, message_id: int, window: int = 5) -> dict:
    """第二层：返回锚定消息 ± window 深上下文。"""
    # 用户点击某条搜索结果后调用
    before_rows = self._execute(
        "SELECT * FROM messages WHERE session_id = ? AND id <= ? ORDER BY id DESC LIMIT ?",
        (session_id, message_id, window + 1)
    )
    after_rows = self._execute(
        "SELECT * FROM messages WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (session_id, message_id, window)
    )
    
    return {
        "window": list(reversed(before_rows)) + list(after_rows),
        "messages_before": max(0, len(before_rows) - 1),
        "messages_after": len(after_rows),
    }
```

**为什么分层？**
- `search_messages()` 返回 10 条结果 × ±1 上下文 = 30 条消息（轻量）
- `get_messages_around()` 返回单条结果 ± 5 上下文 = 11 条消息（按需加载）

## Step 6: 数据迁移

已有数据需要补录到新的 FTS5 表：

```python
def _migrate_fts(self, conn: sqlite3.Connection) -> None:
    """一次性迁移：将已有数据补录到 FTS5 表。"""
    fts_count = conn.execute("SELECT count(*) FROM messages_fts_trigram").fetchone()[0]
    msg_count = conn.execute("SELECT count(*) FROM messages").fetchone()[0]
    
    if fts_count == 0 and msg_count > 0:
        conn.execute("""
            INSERT INTO messages_fts_trigram(rowid, content)
            SELECT id, COALESCE(content, '') || ' ' || COALESCE(tool_name, '') || ' ' || COALESCE(tool_calls, '')
            FROM messages
        """)
```

**触发器只对新数据生效**，已有数据需要手动补录。

## 辅助方法

### CJK 检测

```python
@staticmethod
def _contains_cjk(text: str) -> bool:
    """检测是否包含 CJK 字符。"""
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0xAC00 <= cp <= 0xD7AF):     # Hangul Syllables
            return True
    return False

@classmethod
def _count_cjk(cls, text: str) -> int:
    """统计 CJK 字符数。"""
    return sum(1 for ch in text if cls._is_cjk_codepoint(ord(ch)))

@staticmethod
def _is_cjk_codepoint(cp: int) -> bool:
    """判断是否是 CJK 码位。"""
    return (0x4E00 <= cp <= 0x9FFF or    # CJK Unified Ideographs
            0x3400 <= cp <= 0x4DBF or    # CJK Extension A
            0xAC00 <= cp <= 0xD7AF)      # Hangul Syllables
```

## FTS5 查询语法

```sql
-- 关键词
MATCH 'docker'

-- 短语（精确匹配）
MATCH '"docker 部署"'

-- 布尔
MATCH 'docker AND k8s'
MATCH 'docker OR podman'
MATCH 'docker NOT windows'

-- 前缀
MATCH 'dock*'

-- 列限定
MATCH 'content:docker AND role:user'
```

## 性能优化

1. **WAL 模式**：并发读 + 单写
   ```sql
   PRAGMA journal_mode = WAL;
   ```

2. **索引字段选择**：只索引需要搜索的列，其他用 UNINDEXED 或不放

3. **snippet() 函数**：返回匹配片段，避免加载完整内容
   ```sql
   SELECT snippet(messages_fts, 0, '>>>', '<<<', '...', 40) AS snippet
   ```

4. **LIMIT + OFFSET 分页**：避免一次返回太多结果

## 常见问题

**Q: 为什么 trigram 查询需要 ≥3 CJK 字符？**
A: trigram 是 3 字节窗口，1 个 CJK 字符 = 3 字节，所以至少需要 3 个 CJK 字符（9 字节）才能形成一个 trigram。

**Q: 为什么需要两个 FTS5 表？**
A: unicode61 会把 CJK 字符拆成独立 token，无法做子串匹配。trigram 可以做子串匹配，但索引体积大、拉丁语系查询慢。

**Q: 触发器和手动同步选哪个？**
A: 用触发器。手动同步容易遗漏（特别是有多处写入时），触发器保证一致性。

**Q: search_messages() 和 get_messages_around() 为什么要分开？**
A: 分层设计，避免一次加载太多数据。搜索返回浅上下文（±1），点击后加载深上下文（±5）。

**Q: 为什么我的搜索召回率很低？**
A: 检查 `_sanitize_fts5_query` 是否过度引号化。如果所有术语都被引号化，`docker deployment` 会变成短语匹配（要求相邻），而非 AND 匹配（不要求相邻）。只引号化含 `-` / `.` / `_` 的术语。

**Q: FTS5 表应该放哪些列？**
A: 只放 `content` 一列。如果需要搜索 tool_name 和 tool_calls，应该在触发器中把它们拼接到 content 列。不要用 UNINDEXED 列存储不需要搜索的数据，会增加存储开销。

**Q: UNINDEXED 列有什么用？**
A: UNINDEXED 列的数据会写入 FTS5 表，但不建立倒排索引，不能用于搜索。它只是"搭便车"存在那里，占空间但不参与搜索。通常不推荐使用，除非有特殊需求（如需要在 FTS5 表中保留某些元数据用于调试）。
