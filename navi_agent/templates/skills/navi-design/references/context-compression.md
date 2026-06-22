# 上下文压缩设计参考

## 各项目压缩策略对比

### 触发条件

| 项目 | 触发方式 | 阈值 |
|------|---------|------|
| Claude Code | 时间触发 + 缓存状态 | 缓存冷了（>5分钟）才压缩 |
| KIMI | 比例触发 | 85% 或保留 50K |
| Hermes | 比例触发 | 50% |
| Navi | 比例触发 | 50% |

### 压缩层次

| 项目 | 第一层（不调 LLM） | 第二层（调 LLM） |
|------|-------------------|-----------------|
| Claude Code | Microcompact（工具输出裁剪） | Compaction（LLM 摘要） |
| KIMI | 无 | SimpleCompaction |
| Hermes | 工具输出裁剪 | LLM 摘要 |
| Navi | 工具输出裁剪 | LLM 摘要 |

### 保护区域

| 项目 | 头部保护 | 尾部保护 |
|------|---------|---------|
| Claude Code | 系统提示词 + 第一轮 | 最近 N 个工具结果 |
| KIMI | 无 | 最近 2 条消息 |
| Hermes | 系统提示词 + 前 3 条 | 最近 ~20K token |
| Navi | 系统提示词 + 前 3 轮 | 最近 5% context_window |

### 摘要输出限制

| 项目 | 最小 | 最大 | 计算方式 |
|------|------|------|---------|
| Hermes | 2K | 12K | min(context × 5%, 12K) |
| Navi | 2K | 12K | min(content × 20%, 12K) |

## Navi 实现方案

### 配置参数

```python
TRIGGER_RATIO = 0.50                    # 触发阈值
PROTECT_HEAD_ROUNDS = 3                 # 头部保护轮次
PROTECT_TAIL_RATIO = 0.05               # 尾部保护比例
CLEARED_MESSAGE = "[Old tool result content cleared]"
MIN_SUMMARY_TOKENS = 2_000
MAX_SUMMARY_TOKENS = 12_000
SUMMARY_RATIO = 0.20
```

### 压缩流程

```
1. 每次进入 LLM 节点检查 prompt_tokens
2. 如果 >= context_window × 50%，触发压缩
3. 分割消息：头部 + 中间 + 尾部
4. 预处理中间：旧工具输出 → "[cleared]"
5. LLM 摘要中间部分
6. 拼接：头部 + 摘要 + 尾部
7. 备份旧文件，写入新文件
```

### 文件结构

```
navi_agent/
├── compressor.py          # 上下文压缩器
├── model_router.py        # 模型路由（含压缩模型）
└── config.json            # 配置文件（含 compression 字段）
```

### 配置文件

```json
{
  "compression": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash"
  }
}
```

## 设计决策

### 为什么用 50% 触发？

- 1M context window 下，50% = 500K tokens
- 大多数对话不会超过 100K tokens
- 压缩的主要价值是节省成本，不是防溢出

### 为什么保护前 3 轮？

- 第一轮对话包含用户的初始需求
- 模型需要知道"用户最初想做什么"
- 3 轮约占 7K tokens（5%），可以接受

### 为什么尾部用 5%？

- 1M context × 5% = 52K tokens
- 约 15-20 轮对话
- 保留足够的近期上下文

### 为什么用便宜模型做摘要？

- 输出 token 比输入贵 4 倍
- 用 deepseek-chat 而不是主模型
- 成本降低 ~80%
