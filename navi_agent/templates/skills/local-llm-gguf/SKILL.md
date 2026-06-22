---
name: local-llm-gguf
description: >-
  从 HuggingFace 下载 GGUF 模型并在本地运行（llama.cpp 或 Ollama）。
  当用户问"怎么本地跑模型"、"部署 GGUF"、"llama.cpp 安装"、"本地跑 LLM"、
  "下载 HuggingFace 模型"、"量化模型怎么选"时使用。
  覆盖：llama.cpp 安装（Windows）、GGUF 下载、量化版本选择、
  llama-server/llama-cli 启动参数、Ollama 替代方案、常见问题排查。
  触发词：GGUF、llama.cpp、本地部署、本地运行、量化、Q4_K_M、Q8_0、gguf download、local LLM。
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

## 6. 常见问题排查

| 现象 | 原因 | 修复 |
|---|---|---|
| 输出 `0000...` 重复 | 缺少重复惩罚 | 加 `--repeat-penalty 1.1` |
| 泄漏 `<\|tool_call>` 等原始 token | 未启用 Jinja 模板 | 加 `--jinja` |
| 显存不足 (OOM) | 量化太大或上下文太长 | 降量化 / 减 `--ctx-size` / 减 `--n-gpu-layers` |
| 模型加载失败 | llama.cpp 版本过旧 | 从 GitHub Releases 下载最新版 |
| 新架构无法加载 | 需要特定 build | 检查模型页面的版本要求说明 |

## 7. 采样参数参考

模型作者通常在模型卡片中推荐参数。常见组合：
- **通用对话**：`temp 1.0, top_p 0.95, top_k 64`
- **编码/确定性输出**：`temp 0`（greedy decoding）
- **防重复**：`repeat_penalty 1.1`
