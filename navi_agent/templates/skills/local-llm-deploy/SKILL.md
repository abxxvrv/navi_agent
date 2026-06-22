---
name: local-llm-deploy
description: >
  在本地部署 GGUF 格式的大语言模型，使用 llama.cpp 作为推理引擎。
  当用户问"怎么本地跑模型"、"部署 GGUF"、"llama.cpp 安装"、"本地推理"、
  "怎么用 llama-server"、"模型下载到本地"时使用。
  也适用于：将本地模型接入 OpenAI 兼容客户端（Navi、Open WebUI 等）、
  选择量化版本（Q3/Q4/Q6/Q8）、配置推理参数（temperature、jinja、GPU 层数）。
  触发词：GGUF、llama.cpp、llama-server、本地部署、本地推理、量化模型、
  Q4_K_M、Q3_K_M、ollama vs llama、vllm 对比。
---

# 本地 GGUF 模型部署

## 推理引擎选择

| 引擎 | 适合 | 不适合 |
|---|---|---|
| **llama.cpp** | 个人电脑、小显存(4GB+)、新模型兼容性最好、参数控制精细 | 高并发生产服务 |
| **Ollama** | 快速上手、自动模型管理、多模型调度 | 需要最新架构支持（内置 llama.cpp 版本可能滞后） |
| **vLLM** | 服务器、高并发、24GB+ 显存 | 个人电脑、小显存 |

决策要点：
- 新架构模型（如 `gemma4_unified`）优先选 llama.cpp，更新最快
- Ollama 的 `--location` 参数在 Windows 上经常不生效，装到非 C 盘建议手动下载
- vLLM 需要 CUDA 环境 + 大显存，个人场景不考虑

## 安装 llama.cpp（Windows）

1. 从 GitHub Releases 下载 Vulkan 版 zip（支持独显/核显加速）：
   ```
   https://github.com/ggml-org/llama.cpp/releases
   ```
   选择 `llama-bXXX-bin-win-vulkan-x64.zip`

2. 解压到目标目录（如 `E:\llama.cpp`），约 100MB

3. 加到用户 PATH（PowerShell）：
   ```powershell
   $currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
   $newPath = "$currentPath;E:\llama.cpp"
   [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
   ```

4. 重新开终端验证：`llama-server --version`

## 下载 GGUF 模型

从 HuggingFace 下载指定量化文件：

```bash
# 先查仓库里有哪些文件
curl -sL "https://huggingface.co/api/models/<repo_id>" | python -c "
import sys, json
d = json.load(sys.stdin)
for s in d.get('siblings', []):
    if '.gguf' in s['rfilename'].lower():
        print(s['rfilename'])
"

# 下载指定文件（支持断点续传）
huggingface-cli download <repo_id> <filename.gguf> --local-dir E:/models/
```

大文件（>2GB）建议后台下载或直接用浏览器/aria2。

## 量化版本选择

| 量化 | 显存需求 | 说明 |
|---|---|---|
| Q3_K_M | ~6 GB | 8GB 显存可用 |
| Q4_K_M | ~7 GB | 推荐甜点 |
| Q6_K | ~9 GB | 近无损 |
| Q8_0 | ~12 GB | 基本满质量 |

## 启动 llama-server

```bash
llama-server \
  -m E:/models/<model>.gguf \
  --ctx-size 16384 \
  --n-gpu-layers 99 \
  --no-mmap -fa on \
  --jinja \
  --temp 1.0 --top-p 0.95 --top-k 64 \
  --host 0.0.0.0 --port 18080
```

关键参数：
- `--n-gpu-layers 99`：全部层放 GPU，显存不够就减小
- `--jinja`：**必须开**，否则工具调用特殊 token 会泄漏
- `-fa on`：Flash Attention
- `--alias <name>`：指定 API 中的模型名，客户端调用时用
- `--no-mmap`：Windows 上推荐开启

常见问题：
- 输出 `0000...` 重复 → 加 `--repeat-penalty 1.1`
- 泄漏 `<|tool_call>` 等原始 token → 确认 `--jinja` 已开启
- OOM → 降量化或减小 `--ctx-size`

## 接入 OpenAI 兼容客户端

llama-server 暴露标准 OpenAI API（`http://localhost:<port>/v1`），可直接对接任何支持 OpenAI 格式的客户端。

### Navi 接入

在 `~/.navi/config.json` 的 `providers` 中添加：

```json
"llama": {
  "api_key": "none",
  "base_url": "http://localhost:18080/v1",
  "models": {
    "model-alias": {
      "context_window": 16384
    }
  }
}
```

需要在 `navi_agent/model/router.py` 的 `PROVIDER_CLASSES` 中注册 `LlamaProvider`（不带 thinking/stream_options 等额外参数）。

### 通用接入

任何 OpenAI SDK 客户端：
```python
from openai import OpenAI
client = OpenAI(api_key="none", base_url="http://localhost:18080/v1")
resp = client.chat.completions.create(
    model="model-alias",
    messages=[{"role": "user", "content": "hello"}],
    stream=True,
)
```
