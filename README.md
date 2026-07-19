# Navi Agent

Navi Agent 是一个本地优先的命令行 AI Agent。它在你的项目目录中运行，能读取文件、修改代码、执行命令、管理技能、保存会话历史，并通过交互式 CLI 或微信网关处理任务。

当前分支已经从早期的单文件结构演进为分层结构：CLI、runtime、tools、storage、model、gateway、skills 都各自独立。本文档按当前代码结构描述，不再沿用旧版 `runtime.py` / `tool.py` / `session_store.py` 的说明。

## 快速开始

### 安装

Windows PowerShell：

```powershell
irm https://raw.githubusercontent.com/abxxvrv/navi_agent/gpt/install.ps1 | iex
```

macOS / Linux：

```bash
curl -fsSL https://raw.githubusercontent.com/abxxvrv/navi_agent/gpt/install.sh | bash
```

### 初始化

```powershell
navi init
```

`navi init` 会创建 `~/.navi`，复制默认提示词和内置技能，初始化 `history.sqlite3`，并引导配置主模型和压缩模型。

检查安装状态：

```powershell
navi doctor
```

## 使用

进入交互模式：

```powershell
navi
```

指定工作区：

```powershell
navi --workspace E:\path\to\project
```

运行单次任务并退出：

```powershell
navi run "检查当前工作区的未提交修改"
```

恢复会话：

```powershell
navi --continue
navi --resume <session_id>
```

审批模式：

```powershell
navi --approval strict
navi --approval normal
navi --approval open
navi --yolo
```

`--yolo` 等价于 `--approval open`。

## 交互命令

交互式 CLI 支持以下常用 slash commands：

| 命令 | 作用 |
|---|---|
| `/help` | 查看帮助 |
| `/tools` | 查看当前可用工具 |
| `/skills` | 查看当前可用技能 |
| `/plugins` | 查看已发现插件及启用、信任状态 |
| `/sessions` | 查看最近会话 |
| `/search` | 搜索历史会话 |
| `/model` | 切换 provider / model |
| `/approval` | 查看或切换审批模式 |
| `/compress` | 手动压缩当前会话上下文 |
| `/fork` | 从当前会话分叉 |
| `/mcp` | 管理 MCP server |
| `/clear` | 清屏 |
| `/exit` / `/quit` | 退出 |

## 模型配置

模型配置位于：

```text
~/.navi/config.json
```

当前配置结构以 provider 为中心：

```json
{
  "default_provider": "stepfun",
  "default_model": "step-3.7-flash",
  "compression": {
    "provider": "deepseek",
    "model": "deepseek-v4-flash"
  },
  "providers": {
    "stepfun": {
      "api_key": "...",
      "base_url": "https://api.stepfun.com/v1",
      "models": {
        "step-3.7-flash": { "context_window": 262144 }
      }
    }
  }
}
```

新会话使用 `default_provider` / `default_model`。恢复旧会话时优先使用该会话历史中保存的 provider / model。

### LM Studio 本地模型

先在 LM Studio 中启动本地服务：

```powershell
lms server start --port 1234
```

确认 OpenAI-compatible 接口可用：

```powershell
curl http://localhost:1234/v1/models
```

Navi 中的 LM Studio 配置示例：

```json
{
  "default_provider": "lmstudio",
  "default_model": "your-model-id",
  "compression": {
    "provider": "lmstudio",
    "model": "your-model-id"
  },
  "providers": {
    "lmstudio": {
      "api_key": "lm-studio",
      "base_url": "http://localhost:1234/v1",
      "models": {
        "your-model-id": { "context_window": 32768 }
      }
    }
  }
}
```

`model` 使用 LM Studio 中显示的模型 identifier。工具调用依赖本地模型和服务返回 OpenAI `tool_calls`；如果模型只把工具意图写成普通文本，Navi 不会执行工具。本地模型的 `<think>...</think>` 内容也会按普通正文显示，除非 provider 返回独立的 `reasoning_content` 字段。

### LongCat API

LongCat 使用 OpenAI Chat Completions 兼容接口。通过 `navi init` 选择 `longcat` 并填写 API Key，或手动配置：

```json
{
  "default_provider": "longcat",
  "default_model": "LongCat-2.0",
  "compression": {
    "provider": "longcat",
    "model": "LongCat-2.0"
  },
  "providers": {
    "longcat": {
      "api_key": "...",
      "base_url": "https://api.longcat.chat/openai",
      "models": {
        "LongCat-2.0": { "context_window": 1048576 }
      }
    }
  }
}
```

### Kimi API

Kimi 使用 Moonshot 官方的 OpenAI Chat Completions 兼容接口。通过 `navi init` 选择 `kimi` 并填写 Moonshot API Key，默认模型为 Kimi K3；也可以选择 Kimi K2.7 Code、其高速版本或 Kimi K2.6：

```json
{
  "default_provider": "kimi",
  "default_model": "kimi-k3",
  "compression": {
    "provider": "kimi",
    "model": "kimi-k3"
  },
  "providers": {
    "kimi": {
      "api_key": "...",
      "base_url": "https://api.moonshot.cn/v1",
      "models": {
        "kimi-k3": { "context_window": 1048576, "multimodal": true },
        "kimi-k2.7-code": { "context_window": 262144, "multimodal": true },
        "kimi-k2.7-code-highspeed": { "context_window": 262144, "multimodal": true },
        "kimi-k2.6": { "context_window": 262144, "multimodal": true }
      }
    }
  }
}
```

## 工具系统

主要内置工具包括：

| 工具 | 作用 |
|---|---|
| `glob` | 按模式查找文件 |
| `grep` | 搜索文件内容 |
| `read_file` | 分段读取文本文件 |
| `write_file` | 创建、覆盖或追加文本文件 |
| `patch_file` | 精确替换已有文本 |
| `bash` | 运行短时间、非交互式 Bash 命令 |
| `powershell` | Windows 下运行短时间、非交互式 PowerShell 命令 |
| `vision_analyze` | 辅助分析图片 |
| `search_session` | 搜索 SQLite 会话历史 |
| `skill_view` | 查看技能的 `SKILL.md` |
| `skill_manage` | 管理技能文件 |
| `memory` | 管理长期记忆 |
| `agent` | 管理子 agent |

`skill_manage` 支持：

| action | 作用 |
|---|---|
| `list` | 列出技能 |
| `read` | 读取指定技能 |
| `write` | 创建或整体覆盖技能 |
| `patch` | 定向修补技能内容 |
| `delete` | 删除整个技能目录，目标目录必须包含 `SKILL.md` |

## 技能系统

技能是位于 `~/.navi/skills/<name>/` 下的自包含目录，每个技能至少包含：

```text
SKILL.md
```

`navi init` 会把仓库内置技能从 `navi_agent/templates/skills/` 复制到 `~/.navi/skills/`。运行时系统提示只注入技能索引；需要具体技能时，模型通过 `skill_view` 读取对应的 `SKILL.md`。

后台技能反思会使用 `skill_manage` 维护技能库。目标是保持技能任务针对性强、边界清晰、彼此不重叠；当多个旧技能应合并时，会先创建或更新目标技能，再删除被替代的旧技能。

## 插件

插件兼容 Grok/Claude 的目录约定，可提供 `skills/`、`commands/`、`agents/`、`hooks/hooks.json`、`.mcp.json` 和 `.lsp.json`。manifest 按 `plugin.json`、`.grok-plugin/plugin.json`、`.claude-plugin/plugin.json` 的顺序查找；没有 manifest 时也可按约定目录发现。

临时加载一个可信插件：

```bash
navi --plugin-dir /path/to/plugin
```

持久配置写在 `~/.navi/config.json`：

```json
{
  "plugins": {
    "paths": ["/path/to/plugin"],
    "enabled": ["plugin-name"],
    "disabled": []
  }
}
```

项目 `.navi/plugins`、`.grok/plugins`、`.claude/plugins` 和用户 `~/.navi/plugins` 中的插件默认禁用。项目或外部配置插件还需把插件根目录的规范化绝对路径逐行写入 `~/.navi/trusted-plugins`，才会启动其 Agent、Hooks、MCP 或 LSP；`--plugin-dir` 代表本次会话显式信任。插件命令使用 `/plugin-name:command`，插件技能和 Agent 使用 `plugin-name:name`。

## 会话与记忆

Navi 的本地数据目录是：

```text
~/.navi/
```

常见内容：

| 路径 | 内容 |
|---|---|
| `config.json` | provider、模型和 MCP 配置 |
| `history.sqlite3` | SQLite 会话历史 |
| `skills/` | 已安装技能 |
| `plugins/` | 用户插件 |
| `plugin-data/` | 插件持久数据 |
| `memories/` | 长期记忆 |
| `agents/` | 子 agent 实例 |
| `logs/` | 运行日志 |
| `system.md` | 系统提示词模板 |
| `compact-prompt.md` | 压缩提示词 |
| `memory-review-prompt.md` | 记忆反思提示词 |
| `skill-review-prompt.md` | 技能反思提示词 |

历史搜索使用 `history.sqlite3`，包含 FTS5 索引、会话列表、锚定窗口和压缩会话 lineage。

## 微信网关

Navi 可以通过 iLink 接入微信：

```powershell
navi weixin login
navi weixin start --account <account_id>
```

访问控制默认 fail-closed。授权用户：

```powershell
navi weixin allow <user_id> --account <account_id>
```

移除授权：

```powershell
navi weixin deny <user_id> --account <account_id>
```

查看白名单：

```powershell
navi weixin allowlist --account <account_id>
```

微信网关支持文本、图片、文件、视频和语音文件路径注入。聊天中可发送 `/new` 开启新对话，用 `/model list` 查看提供商和模型名称列表，或用 `/model <provider> <modelname>` 切换模型；只有这三种精确格式会作为网关命令处理，其他以 `/` 开头的内容仍作为普通消息交给模型。运行中可发送 `!cancel` 请求取消当前任务。较长任务执行期间，网关会使用 iLink typing ticket 周期性发送“正在输入中”状态。

## QQ 网关

Navi 也可以通过 QQ 官方机器人开放平台接入 QQ，体验与配置和微信网关一致，区别仅在于接入方式（WebSocket 网关 + REST API）：

```powershell
navi qq login
navi qq start --account <account_id>
```

`navi qq login` 通过手机 QQ 扫码完成配置：扫码后服务端会把 AppID 与（本地解密的）AppSecret 保存到 `~/.navi/qq/accounts/`，扫码用户自动加入白名单。

访问控制同样默认 fail-closed，用法与微信一致：

```powershell
navi qq allow <user_id> --account <account_id>
navi qq deny <user_id> --account <account_id>
navi qq allowlist --account <account_id>
```

QQ 网关同时处理私聊（C2C）和群聊（@机器人）消息，支持文本、图片、文件、视频和语音文件路径注入。聊天中可发送 `/new` 开启新对话，用 `/model list` 查看提供商和模型名称列表，或用 `/model <provider> <modelname>` 切换模型；只有这三种精确格式会作为网关命令处理，其他以 `/` 开头的内容仍作为普通消息交给模型。运行中可发送 `!cancel` 请求取消当前任务。

群聊按群维度单独授权（同样 fail-closed，未授权的群会被静默忽略），加 `--group` 即可：

```powershell
navi qq allow <group_openid> --group --account <account_id>
navi qq deny <group_openid> --group --account <account_id>
navi qq allowlist --group --account <account_id>
```

注意：QQ 的 `group_openid` **不是群号**，而是平台针对「每个机器人 + 每个群」生成的加密 id，无法从群号推导，只能从收到的群消息里获取。获取流程：先在群里 @一次机器人，未授权时网关**不会在群里回复**，但会在 `navi qq start` 的终端打印该群的 `group_openid`（含可直接复制的授权命令）；复制执行 `allow ... --group` 后，之后该群的消息即生效。私聊同理——未授权时机器人会私信告知用户自己的 openid。

## Web UI

`navi web` 在本机启动一个网页控制台，聊天能力与 CLI 一致（流式输出、思考过程、工具卡片、审批、中断、会话恢复、模型切换），并可在页面上启动/停止 QQ 和微信网关（扫码登录仍需在终端完成）：

```powershell
navi web                       # 默认 127.0.0.1:8788，审批 normal
navi web -p 8080 -w ./proj --approval strict
```

启动后终端会打印带一次性访问 token 的 URL，用浏览器打开即可。服务默认只绑定 127.0.0.1；`--host` 改绑其他地址前请先想清楚风险——网页里的 agent 拥有和本地 CLI 相同的文件与命令执行权限。

## MCP

交互式 CLI 内置 `/mcp` 命令，用于管理 MCP server：

```text
/mcp status
/mcp add
/mcp remove
/mcp reload
/mcp help
```

MCP 配置写入 `~/.navi/config.json`。

## 当前仓库结构

```text
navi_agent/
├── cli/
│   ├── main.py              # Typer 入口和 slash command 桥接
│   ├── chat_controller.py   # 交互式 CLI 编排层
│   ├── prompt_ui.py         # prompt_toolkit 输入层
│   └── stream_box.py        # 流式输出框
├── runtime/
│   ├── agent.py             # 主 agent loop、工具调度、审批、历史写入
│   ├── interrupt_scope.py   # 单轮中断作用域
│   ├── interruptible.py     # 模型、审批、工具 worker 的阻塞包装
│   ├── background_review.py # 记忆/技能后台反思
│   └── sub_agent.py         # 轻量子 agent
├── model/
│   ├── router.py            # provider/router
│   └── request.py           # 可中断模型请求 worker
├── tools/
│   ├── builtin.py           # 内置工具实现
│   ├── registry.py          # 工具注册表
│   ├── approval.py          # 审批策略
│   └── approval_broker.py   # runtime/UI 审批桥
├── storage/
│   ├── history_store.py     # SQLite 会话历史
│   ├── memory_store.py      # 长期记忆
│   └── agent_store.py       # 子 agent 存储
├── context/
│   ├── context_manager.py   # 运行上下文组装
│   └── compressor.py        # 上下文压缩
├── gateway/
│   ├── ilink.py             # iLink 协议 helper
│   ├── weixin.py            # 微信网关 adapter
│   ├── qqbot.py             # QQ 协议 helper
│   └── qq.py                # QQ 网关 adapter
├── integrations/
│   ├── mcp_client.py
│   └── mcp_commands.py
├── skills/
│   └── skill_manage.py
└── templates/
    ├── navi_home/
    └── skills/
```

## 开发

运行测试：

```powershell
python -m pytest
```

运行某个测试文件：

```powershell
python -m pytest tests/test_skill_manage.py
```

检查当前安装：

```powershell
navi doctor
```

## 许可证

当前仓库尚未添加 LICENSE 文件。添加许可证前，默认保留所有权利。
