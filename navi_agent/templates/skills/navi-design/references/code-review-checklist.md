# Navi 代码审查清单

## 容易犯错的点

### 1. 字段名一致性

检查函数参数名和返回值字段名是否一致。

```python
# ❌ 错误：参数名是 path，但返回值用 directory
def glob(self, path: str = "."):
    return {"ok": False, "error": "...", "directory": path}

# ✅ 正确：保持一致
def glob(self, path: str = "."):
    return {"ok": False, "error": "...", "path": path}
```

### 2. 死代码

检查每个变量是否被使用，特别是计算后没有传递的值。

```python
# ❌ 错误：budget 计算了但没使用
def _generate_summary(self, middle):
    budget = max(MIN, min(content_tokens * RATIO, MAX))
    return self._call_llm(prompt)  # 没有传 budget

# ✅ 正确：budget 传递给函数
def _generate_summary(self, middle):
    budget = max(MIN, min(content_tokens * RATIO, MAX))
    return self._call_llm(prompt, max_tokens=int(budget * 1.3))
```

### 3. 函数调用链

检查参数是否完整传递到最终调用。

```python
# ❌ 错误：max_tokens 没有传递给 API
def _call_llm(self, prompt):
    response = self.router.chat_stream_compression(messages=[...])

# ✅ 正确：完整传递
def _call_llm(self, prompt, max_tokens):
    response = self.router.chat_stream_compression(
        messages=[...],
        max_tokens=max_tokens,
    )
```

### 4. 测试数据大小

测试数据要足够大，能触发逻辑分支。

```python
# ❌ 错误：100 条消息不够测试尾部保护（预算 200K chars）
messages = [{"role": "user", "content": "x" * 1000} for _ in range(100)]

# ✅ 正确：300 条消息才能测试
messages = [{"role": "user", "content": "x" * 1000} for _ in range(300)]
```

### 5. 私有方法调用

调用私有方法（_ 开头）虽然能工作，但违反封装。优先使用公开方法。

```python
# ⚠️ 可接受：没有公开方法时
self.session_store._write_meta()

# ✅ 更好：添加公开方法
class SessionStore:
    def save_meta(self):
        self._write_meta()
```

## 审查流程

1. **检查字段名一致性** — 参数名、返回值、配置字段
2. **检查死代码** — 每个变量是否被使用
3. **检查函数调用链** — 参数是否完整传递
4. **检查测试覆盖** — 测试数据是否足够大
5. **运行测试** — 确保没有破坏现有功能
