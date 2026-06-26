---
name: local-llm-gguf
description: >-
  从 HuggingFace 下载 GGUF 模型并在本地运行（llama.cpp 或 Ollama）。
  当用户问"怎么本地跑模型"、"部署 GGUF"、"llama.cpp 安装"、"本地跑 LLM"、
  "下载 HuggingFace 模型"、"量化模型怎么选"时使用。
  也适用于：检查本地 LLM 环境（ollama/llama.cpp 版本、安装路径、模型路径）、
  迁移 Ollama 模型到其他磁盘、配置 OLLAMA_MODELS 环境变量。
  覆盖：llama.cpp 安装（Windows）、GGUF 下载、量化版本选择、
  llama-server/llama-cli 启动参数、Ollama 替代方案、常见问题排查。
  也适用于：计算显存与上下文长度关系、估算模型显存需求、优化显存使用。
  触发词：GGUF、llama.cpp、本地部署、本地运行、量化、Q4_K_M、Q8_0、gguf download、local LLM、
  ollama 版本、ollama 路径、模型迁移、OLLAMA_MODELS、显存、上下文长度、VRAM、GPU内存、8GB显存。
---

# 本地运行 GGUF 模型

## 1. 量化版本选择

| 量化 | 大小(12B) | 适合场景 |
|---|---|---|
| Q3_K_M | ~5.7 GB | 8 GB 显存，最小可靠量化 |
| Q4_K_M | ~6.9 GB | 推荐甜点，质量/大小平衡 |
| Q6_K | ~9.1 GB | 近无损 |
| Q8_0 | ~11.8 GB | 基本满质量 |
| F16/BF16 | ~24 GB | 全精度，需大显存 |

提示：Q2_K 通常不可靠，除非作者明确验证过。选量化时先看用户显存，再推荐。

## 2. 下载 GGUF 文件

前置：`pip show huggingface-hub` 确认已安装，否则 `pip install huggingface-hub`。

### HuggingFace 缓存机制

默认缓存目录：`~/.cache/huggingface/hub/`（Windows: `C:\Users\<user>\.cache\huggingface\hub\`）。

目录结构：
```
hub/
├── models--<org>--<name>/
│   ├── blobs/          # 实际模型文件（大文件）
│   ├── snapshots/      # 指向 blobs 的符号链接/副本
│   └── refs/           # 分支引用
└── version.txt
```

**改变默认缓存位置**（环境变量）：
```powershell
# 方式1：改整个 HF 缓存根目录
[Environment]::SetEnvironmentVariable('HF_HOME', 'E:\huggingface', 'User')

# 方式2：只改 hub 缓存
[Environment]::SetEnvironmentVariable('HUGGINGFACE_HUB_CACHE', 'E:\huggingface\hub', 'User')
```

**指定路径下载**（不走缓存）：
```bash
huggingface-cli download <repo_id> --local-dir /e/models/<name>
```

**验证缓存中的模型完整性**：不能只看目录大小，要检查 `blobs/` 或 `snapshots/` 里是否有实际的模型文件（.safetensors / .bin）：
```bash
# 检查 blobs
ls -lah ~/.cache/huggingface/hub/models--<org>--<name>/blobs/

# 或检查 snapshots 中的实际文件
find ~/.cache/huggingface/hub/models--<org>--<name>/snapshots/ -name "*.safetensors" -o -name "*.bin"
```

空壳（只有 refs/ 和空 blobs/）说明下载中断，可安全删除。

**清理不完整的缓存**：
```bash
rm -rf ~/.cache/huggingface/hub/models--<org>--<name>
```

```bash
# 下载特定量化文件（--include 按文件名过滤）
huggingface-cli download <repo_id> \
  --include "*Q4_K_M.gguf" \
  --local-dir C:/models/

# 示例
huggingface-cli download yuxinlu1/gemma-4-12B-v2-GGUF \
  --include "*Q4_K_M.gguf" \
  --local-dir C:/models/
```

### 列出仓库中的可用文件

HuggingFace REST API（Windows 用 `python`，非 `python3`）：

```bash
curl -sL "https://huggingface.co/api/models/<repo_id>" \
  | python -c "import sys,json; [print(s['rfilename']) for s in json.load(sys.stdin).get('siblings',[])]"
```

过滤特定量化：
```bash
curl -sL "https://huggingface.co/api/models/<repo_id>" \
  | python -c "import sys,json; [print(s['rfilename']) for s in json.load(sys.stdin).get('siblings',[]) if 'Q4' in s['rfilename'].upper()]"
```

## 3. 安装 llama.cpp（Windows）

### 方式一：直接下载解压（推荐，可控安装路径）

从 GitHub Releases 下载预编译 zip（Vulkan 版，支持独显/核显加速）：

```bash
mkdir -p /e/llama.cpp
curl -L -o /e/llama.cpp/llama.zip \
  "https://github.com/ggml-org/llama.cpp/releases/download/b9733/llama-b9733-bin-win-vulkan-x64.zip"
cd /e/llama.cpp && unzip llama.zip && rm llama.zip
```

包大小约 37 MB（zip），解压后约 110 MB。版本号随 releases 更新。

### 方式二：WinGet

```bash
winget install llama.cpp
```

注意：WinGet 的 `--location` 参数在 Windows 上经常不生效。想装到自定义路径用方式一。

### 方式三：Ollama（上层封装，更简单但参数控制少、版本更新滞后）

```bash
winget install Ollama.Ollama
```

Ollama 内置 llama.cpp，新架构模型（如 gemma4_unified）可能因内置版本过旧而加载失败。

## 4. 加到 PATH（Windows 用户级）

参考 `windows-cli-global-access` 技能。PowerShell：

```powershell
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$newPath = "$currentPath;E:\llama.cpp"
[Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
```

加完后重新打开终端生效。

## 5. 启动模型

### llama-server（OpenAI 兼容 API 服务，推荐）

```bash
llama-server \
  -m <model.gguf> \
  --ctx-size 16384 \
  --n-gpu-layers 99 \
  --no-mmap -fa on \
  --jinja \
  --temp 1.0 --top-p 0.95 --top-k 64 \
  --host 0.0.0.0 --port 18080
```

关键参数说明：
- `--n-gpu-layers 99`：全部层放 GPU，显存不够减小此值
- `--jinja`：**必须开**，否则工具调用的特殊 token 会泄漏
- `-fa on`：Flash Attention，提升性能
- `--no-mmap`：某些模型/GPU 组合需要

启动后访问 `http://localhost:18080`，兼容 OpenAI API 格式。

### llama-cli（终端直接对话）

```bash
llama-cli \
  -m <model.gguf> \
  --ctx-size 16384 \
  --n-gpu-layers 99 \
  --jinja \
  --temp 1.0 --top-p 0.95 --top-k 64 \
  -cnv
```

### Ollama 方式

创建 Modelfile：
```
FROM /path/to/model.gguf
PARAMETER temperature 1.0
PARAMETER top_p 0.95
PARAMETER top_k 64
PARAMETER repeat_penalty 1.1
```

```bash
ollama create my-model -f Modelfile
ollama run my-model
```

## 6. 显存与上下文长度计算

当用户问"我的显存能支持多长上下文"、"8GB显存能跑多大模型"时使用。

### 显存占用组成

1. **模型权重**：参数量 × 每个参数字节数
   - 4B参数，bfloat16精度：4B × 2字节 = 8GB
   - 4B参数，Q4_K_M量化：约2-3GB

2. **KV缓存**：这是决定上下文长度的关键
   - 计算公式：`2 × 层数 × 头数 × 头维度 × 上下文长度 × 每个元素字节数`
   - 对于Qwen3-4B：36层，8个KV头，头维度=2560/32=80
   - KV缓存大小 = 2 × 36 × 8 × 80 × 上下文长度 × 2字节

3. **激活内存**：推理时的临时内存，通常较小

4. **系统开销**：CUDA内核、框架等，通常1-2GB

### 计算示例（Qwen3-4B，bfloat16）

假设RTX 4070 8GB显存：

1. **模型权重**：8GB（bfloat16）
2. **剩余显存**：8GB - 8GB = 0GB（无法运行）

**解决方案**：使用量化模型

1. **Q4_K_M量化**：约2.5GB
2. **剩余显存**：8GB - 2.5GB = 5.5GB
3. **系统开销**：预留1.5GB
4. **可用于KV缓存**：4GB

计算最大上下文长度：
```
KV缓存 = 2 × 36层 × 8头 × 80维度 × 上下文长度 × 2字节
4GB = 2 × 36 × 8 × 80 × 上下文长度 × 2
上下文长度 = 4GB / (2 × 36 × 8 × 80 × 2)
上下文长度 ≈ 4,340 tokens
```

### 实际建议

对于8GB显存（RTX 4070）：
- **4B模型（Q4_K_M量化）**：约4K-8K上下文
- **4B模型（Q8_0量化）**：约2K-4K上下文
- **7B模型（Q4_K_M量化）**：约2K-4K上下文

### 优化策略

1. **使用量化模型**：Q4_K_M是甜点，Q8_0质量更高但显存更大
2. **减少GPU层数**：`--n-gpu-layers` 部分层放CPU
3. **减小上下文长度**：`--ctx-size` 直接限制
4. **启用Flash Attention**：`-fa on` 减少KV缓存显存
5. **使用GQA模型**：Qwen3的GQA（8个KV头）比MHA更省显存

### 快速估算公式

对于Qwen3-4B（GQA，8个KV头）：
```
KV缓存(GB) ≈ 上下文长度 × 0.001
总显存(GB) ≈ 模型大小(GB) + KV缓存(GB) + 1.5
```

示例：4B Q4_K_M（2.5GB）+ 8K上下文（0.008GB）+ 1.5 ≈ 4GB

### 专用子代理模型示例

**FastContext-1.0-4B-SFT**：专门用于代码库探索的子代理
- 基于Qwen3-4B-Instruct微调
- 支持262K上下文（实际受显存限制）
- 只读工具调用：READ、GLOB、GREP
- 减少主代理token消耗60%

**在RTX 4070 8GB显存下的估算**：
- 模型大小：Q4_K_M量化约2.5GB
- 系统开销：1.5GB
- 可用于KV缓存：4GB
- 估算上下文长度：约4K-8K tokens

**优化建议**：
- 使用Q4_K_M量化（质量/大小平衡）
- 启用Flash Attention减少KV缓存显存
- 根据实际任务调整上下文长度

### LM Studio 服务器配置

当用户问"怎么配置LM Studio服务器"、"LM Studio API端点"、"SmallCode连接LM Studio"时使用。

**LM Studio服务器启动**：
1. 打开LM Studio应用
2. 加载模型（推荐8B-35B参数）
3. 启动本地服务器（默认端口1234）

**服务器端点**：
```
GET  http://localhost:1234/v1/models          # 列出可用模型
POST http://localhost:1234/v1/chat/completions # 聊天补全
POST http://localhost:1234/v1/completions      # 文本补全
POST http://localhost:1234/v1/embeddings       # 嵌入向量
```

**连接配置示例**（SmallCode）：
```bash
# .env文件
SMALLCODE_MODEL=qwen2.5-coder-7b-instruct
SMALLCODE_BASE_URL=http://localhost:1234/v1
SMALLCODE_PROVIDER=openai
```

**测试连接**：
```bash
# 测试服务器是否运行
curl -s http://localhost:1234/v1/models

# 测试聊天补全
curl -s http://localhost:1234/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"qwen2.5-coder-7b-instruct","messages":[{"role":"user","content":"hello"}]}'
```

**常见问题**：
1. **连接被拒绝**：检查LM Studio是否启动服务器，防火墙是否阻止
2. **模型不支持**：确保模型支持函数调用（tool use）
3. **端口冲突**：修改LM Studio端口或更新配置

**网络配置**：
- 默认绑定到`0.0.0.0:1234`，允许局域网访问
- 如果需要外部访问，检查防火墙设置
- 虚拟机用户可能需要配置网络桥接

## 7. 常见问题排查

| 现象 | 原因 | 修复 |
|---|---|---|
| 输出 `0000...` 重复 | 缺少重复惩罚 | 加 `--repeat-penalty 1.1` |
| 泄漏 `<\|tool_call>` 等原始 token | 未启用 Jinja 模板 | 加 `--jinja` |
| 显存不足 (OOM) | 量化太大或上下文太长 | 降量化 / 减 `--ctx-size` / 减 `--n-gpu-layers` |
| 模型加载失败 | llama.cpp 版本过旧 | 从 GitHub Releases 下载最新版 |
| 新架构无法加载 | 需要特定 build | 检查模型页面的版本要求说明 |

## 7. LM Studio 模型导入

当用户问"LM Studio 识别不了模型"、"怎么把 GGUF 导入 LM Studio"、"lms import 怎么用"时使用。

### LM Studio 目录结构要求

LM Studio 期望的模型目录结构：
```
~/.lmstudio/models/
└── publisher/
    └── model/
        └── model-file.gguf
```

例如：`~/.lmstudio/models/stefancosma/Qwen2.5-Coder-7B-Instruct-Q4_K_M-GGUF/qwen2.5-coder-7b-instruct-q4_k_m.gguf`

### 使用 lms import 导入

`lms` CLI 位于 `~/.lmstudio/bin/lms.exe`，导入命令：

```bash
lms import <path/to/model.gguf>
```

常用选项：
| 选项 | 说明 |
|---|---|
| `-y, --yes` | 自动批准所有提示，自动从文件名解析 publisher/model |
| `-c, --copy` | 复制文件（保留原始文件） |
| `-L, --hard-link` | 创建硬链接（推荐，保留原始文件，无需管理员权限） |
| `-l, --symbolic-link` | 创建符号链接（Windows 需管理员权限，通常失败） |
| `--dry-run` | 预览操作，不实际执行 |
| `--user-repo <user/repo>` | 手动指定 publisher/model 名称 |

**推荐用法**（Windows）：
```bash
# 硬链接，保留原始文件位置，无需管理员权限
lms import -y -L /e/models/Qwen2.5-Coder-7B-Instruct-GGUF/qwen2.5-coder-7b-instruct-q4_k_m.gguf
```

### Windows 符号链接问题

Windows 创建符号链接默认需要管理员权限。`lms import -l` 通常会失败：
```
EPERM: operation not permitted, symlink '...' -> '...'
```

解决方案：使用 `-L`（硬链接）替代 `-l`（符号链接）。硬链接不占用额外磁盘空间，且无需管理员权限。

### 其他 lms 命令

```bash
# 列出已导入的模型
lms ls

# 查看帮助
lms --help
lms import --help
```

### 排查模型不显示

1. 确认目录结构正确（publisher/model/file.gguf）
2. 使用 `lms ls` 检查是否已导入
3. 重启 LM Studio 应用刷新模型列表
4. 如果是手动放置的文件，用 `lms import -L` 重新导入

## 8. 检查本地 LLM 环境

当用户询问"我电脑上 ollama/llama 是什么版本"、"安装在哪"、"模型装在哪"时使用。

### Ollama

```bash
# 版本
ollama --version

# 安装路径
where ollama
# Windows 默认: C:\Users\<user>\AppData\Local\Programs\Ollama\

# 模型存储路径
ls ~/.ollama/models/
# Windows 默认: C:\Users\<user>\.ollama\models\

# 已下载模型列表
ollama list
```

### llama.cpp

```bash
# 安装路径
where llama-server
# 通常在用户指定目录，如 E:\llama.cpp\

# GGUF 模型文件
find /c/Users/<user> -maxdepth 5 -iname "*.gguf" 2>/dev/null
find /e -maxdepth 4 -iname "*.gguf" 2>/dev/null
```

### LM Studio

```bash
# 后端路径
ls ~/.lmstudio/extensions/backends/
# 包含多个 llama.cpp 版本（avx2、cuda、vulkan）

# CLI 工具
ls ~/.lmstudio/bin/lms.exe

# 已导入模型
lms ls
```

**找到 LM Studio GUI 安装路径**（可能不在默认位置）：
```powershell
# 通过开始菜单快捷方式找到实际安装路径
$sh = New-Object -ComObject WScript.Shell
$lnk = $sh.CreateShortcut("$env:APPDATA\Microsoft\Windows\Start Menu\Programs\LM Studio.lnk")
$lnk.TargetPath
# 示例输出: D:\LMS\LM Studio\LM Studio.exe
```

**获取版本信息**：
```powershell
(Get-Item "D:\LMS\LM Studio\LM Studio.exe").VersionInfo | Select-Object FileVersion, ProductVersion
```

## 8. 模型迁移

### Ollama 模型迁移到其他磁盘

Ollama 不会自动迁移已有模型。手动步骤：

```bash
# 1. 复制模型到新位置
cp -r ~/.ollama/models /e/ollama/models

# 2. 设置环境变量（PowerShell）
[Environment]::SetEnvironmentVariable('OLLAMA_MODELS', 'E:\ollama\models', 'User')

# 3. 重启 ollama 服务

# 4. 验证新位置生效后删除旧目录
rm -rf ~/.ollama/models
```

### 注意事项

- 设置 `OLLAMA_MODELS` 只改变新下载模型的存储位置，已有模型需手动复制
- Ollama 的 `--location` 参数在 Windows 上经常不生效，建议用环境变量
- llama.cpp 无固定模型目录，用户自行管理

## 9. 采样参数参考

模型作者通常在模型卡片中推荐参数。常见组合：
- **通用对话**：`temp 1.0, top_p 0.95, top_k 64`
- **编码/确定性输出**：`temp 0`（greedy decoding）
- **防重复**：`repeat_penalty 1.1`
