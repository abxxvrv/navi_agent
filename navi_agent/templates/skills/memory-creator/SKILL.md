---
name: memory-creator
description: 创建或修改当前项目的项目记忆文件。当用户要求记住某个项目事实、用户纠正你的做法、或你经过高代价探索获得项目相关发现时使用。
metadata:
  short-description: Create or update project memory
---

# Memory Creator — 项目记忆规范

项目记忆是你在当前项目里积累的经验事实：项目约定、常用验证命令、架构边界、运行环境、踩过的坑。它随项目走，下个会话的你会在系统提示词里看到索引。

## 存放位置与结构

```
<工作目录>/.navi/memories/
├── PROJECT_memory.md       # 索引，系统自动生成，禁止手动创建或修改
└── <记忆名>.md             # 一条记忆一个文件，用 write_file / patch_file 操作
```

## 文件模板

新建记忆文件时逐字照抄此结构（frontmatter 缺 name 或 description 的文件不会进入索引）：

```markdown
---
name: <kebab-case短名，与文件名一致>
description: <一句话钩子，100字符内>
origin_session: <当前会话ID，未知则省略此行>
updated: <今天日期，如 2026-07-03>
---

正文。写清事实本身、为什么如此、验证方法。
可用 [[另一条记忆的name]] 引用相关记忆。
```

## 操作规范

1. **写前先看索引**（系统提示词的"项目记忆"段）：已有相近记忆就用 patch_file 修改合并，不要新建重复文件；修改正文时同步更新 `updated` 字段。
2. **文件名**：kebab-case（小写字母、数字、连字符），与 frontmatter 的 name 一致；禁止占用 `PROJECT_memory.md` 和 `PROJECT.txt`。
3. **description 是检索线索**：下个会话的你只能看到这一行，据此决定是否读正文。写"钩子"不写"标题"——"QQ 网关错误码含义与 media upload 的坑" 好于 "网关笔记"。
4. **控制总量**：索引超过 40 行时，先合并或删除旧记忆再新增。
5. 新写入的记忆下个会话才会进入索引，属正常现象，不需要验证或修复。

## 什么值得存

- 项目约定、常用验证命令、架构边界、运行环境
- 高代价发现：花了很长时间才查明的事实、反直觉的坑
- 用户明确要求记住的项目相关信息

## 不要存

- 任务进度、临时状态
- 敏感信息（密码、密钥）
- 容易重新发现的信息（读一下代码就知道的东西）
- 跨项目通用经验 → 用 memory 工具写 MEMORY.md
- 用户偏好与习惯 → 用 memory 工具写 USER.md
- 可复用的工作流程 → 用 skill-creator 建技能
