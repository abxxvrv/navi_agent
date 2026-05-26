# Navi Agent

> 本地项目导航员。把读文件、改文件、跑命令、加载技能、记录会话，收进一个可交互的 CLI Agent。

[![Python](https://img.shields.io/badge/Python-3.11%2B-blue)](pyproject.toml)
[![CLI](https://img.shields.io/badge/CLI-Typer%20%2B%20Rich-green)](navi_agent/cli.py)
[![Runtime](https://img.shields.io/badge/Runtime-LangGraph-purple)](navi_agent/runtime.py)
[![Model](https://img.shields.io/badge/Model-DeepSeek%20compatible-orange)](#环境变量)
[![Skills](https://img.shields.io/badge/Skills-Bundled%20Loader-black)](#技能系统)

**Navi Agent 是一个本地优先的命令行编程助手。**

它不是一个网页应用，也不是一个只会聊天的壳。Navi 的目标很直接：在你的项目目录里理解上下文，调用受控工具，完成文件阅读、代码修改、命令验证和技能加载。

[效果示例](#效果示例) · [安装](#安装) · [技能系统](#技能系统) · [工作逻辑](#工作逻辑) · [仓库结构](#仓库结构)

---

## 当前版本

### v0.1.0：本地 CLI Agent 骨架

这一版重点是把 Agent 的基本工作闭环跑通：

| 模块 | 能力 | 文件 |
|---|---|---|
| CLI 入口 | 交互式对话、slash commands、Rich 输出 | `navi_agent/cli.py` |
| Agent Runtime | LangGraph 工具循环、模型调用、事件分发 | `navi_agent/runtime.py` |
| 工具系统 | 文件、命令、技能、会话搜索工具 | `navi_agent/tool.py` |
| 会话存储 | turns/events/index 三类历史记录 | `navi_agent/session_store.py` |
| 上下文构造 | 系统提示、环境信息、技能内容注入 | `navi_agent/context_manager.py` |

设计原则：

1. **本地优先**：默认围绕本机项目目录工作。
2. **工具显式**：读、写、运行命令都通过工具层完成。
3. **技能可扩展**：通过 `skills/` 目录加载任务专用说明。
4. **历史可追溯**：会话和工具执行轨迹保存在本地。

---

## 工作逻辑

```text
用户输入
  ↓
CLI 收集请求
  ↓
AgentRuntime 构造上下文
  ↓
模型决定是否调用工具
  ↓
ToolRegistry 执行本地工具
  ↓
结果写入 session history
  ↓
CLI 输出最终回答
```

核心路径：

```text
cli.py
  → runtime.py
    → context_manager.py
    → tool_registry.py
    → tool.py
    → session_store.py
```

---

## 效果示例

### 查看项目并修改代码

```text
用户 ❯ 帮我看看 CLI 为什么把 Markdown 标题居中了

Navi ❯ 读取 cli.py，发现最终回答通过 Rich Markdown 渲染。
       Rich 的 Heading 默认 text.justify = "center"，
       所以 `## 会话...` 会被居中显示。
```

### 加载技能

```text
用户 ❯ 使用 docx 技能帮我分析这个 Word 文档

Navi ❯ 发现任务需要文档处理能力。
       加载 navi_agent/skills/docx/SKILL.md。
       下一轮将按该技能的工作流处理文档。
```

### 搜索历史

```text
用户 ❯ 刚才那个 git clone 为什么失败？

Navi ❯ 搜索当前会话事件轨迹。
       找到 run_command 返回 timed_out / exit_code=None，
       说明命令没有在工具超时时间内正常结束。
```

---

## 安装

### 1. 克隆仓库

```powershell
git clone https://github.com/abxxvrv/navi_agent.git
cd navi_agent
```

### 2. 创建虚拟环境

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
```

### 3. 安装项目

```powershell
pip install -e .
```

### 4. 配置环境变量

在仓库根目录创建 `.env`：

```env
DEEPSEEK_API_KEY=your_api_key_here
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`DEEPSEEK_BASE_URL` 可省略，默认值为 `https://api.deepseek.com`。

---

## 使用

启动交互模式：

```powershell
navi
```

指定工作区：

```powershell
navi --workspace E:\path\to\project
```

常用命令：

| 命令 | 作用 |
|---|---|
| `/help` | 查看帮助 |
| `/tools` | 查看可用工具 |
| `/skills` | 查看可用技能 |
| `/sessions` | 查看最近会话 |
| `/clear` | 清屏 |
| `/exit` | 退出 |

---

## 技能系统

Navi 会从源码目录下的 `navi_agent/skills` 加载技能：

```text
navi_agent/skills/
├── docx/
├── frontend-design/
└── skill-creator/
```

每个技能目录至少包含：

```text
SKILL.md
```

加载流程：

1. 模型判断当前任务是否需要某个技能。
2. 调用 `load_skill`。
3. 下一次模型调用时，`SKILL.md` 内容进入系统上下文。
4. 工具和回答按技能说明执行。

---

## 工具能力

| 工具 | 用途 |
|---|---|
| `list_dir` | 列出目录 |
| `read_file` | 读取文件片段 |
| `write_file` | 写入文件 |
| `patch_file` | 精确替换文件内容 |
| `run_command` | 执行短时间、非交互式命令 |
| `skill_view` | 查看技能内容 |
| `load_skill` | 激活技能 |
| `search_session_history` | 搜索历史会话和工具轨迹 |

---

## 仓库结构

```text
navi_agent/
├── README.md
├── pyproject.toml
├── navi_agent/
│   ├── cli.py
│   ├── runtime.py
│   ├── tool.py
│   ├── tool_registry.py
│   ├── context_manager.py
│   ├── session_store.py
│   ├── history_utils.py
│   └── skills/
│       ├── docx/
│       ├── frontend-design/
│       └── skill-creator/
└── .gitignore
```

---

## 本地数据

Navi 会在本地保存会话记录：

```text
.light_agent/sessions/
```

本地数据、虚拟环境、缓存和密钥文件不会提交到 git：

```text
.env
venv/
.light_agent/
.navi/
__pycache__/
*.egg-info/
```

---

## 路线图

- 更清晰的技能安装工具
- 更细粒度的工具权限配置
- 更完整的中断恢复机制
- 更好的 Markdown 终端渲染控制
- 更稳定的跨工作区运行体验

---

## 许可证

当前仓库尚未添加 LICENSE 文件。添加许可证前，默认保留所有权利。
