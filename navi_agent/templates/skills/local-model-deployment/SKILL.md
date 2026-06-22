---
name: local-model-deployment
description: 在本地部署 GGUF 模型并通过 llama.cpp 提供推理服务，接入 Navi 作为 provider。当用户说"本地跑模型"、"部署 GGUF"、"llama.cpp"、"llama-server"、"本地推理"、"接本地模型"、"ollama 对接 navi"时使用。也适用于：选择量化版本、估算 VRAM 上下文容量、配置 Navi 的自定义 model provider。
---

# 本地模型部署

将 HuggingFace 上的 GGUF 模型部署到本地，通过 llama-server 提供 OpenAI 兼容 API，并接入 Navi。

## 1. 安装 llama.cpp

优先手动下载解压到用户指定目录（避免 WinGet 的 `--location` 不生效问题）。

```bash
# 下载 Windows Vulkan 预编译包（版本号随更新变化）
curl -L -o /e/llama.cpp/llama.zip "https://github.com/ggml-org/llama.cpp/releases/download/b9733/llama-b9733-bin-win-vulkan-x64.zip"
cd /e/llama.cpp && unzip -o llama.zip -d . && rm llama.zip
```

加到用户 PATH（PowerShell）：
```powershell
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$newPath = "$currentPath;E:\llama.cpp"
[Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
```

验证：`llama-server.exe --version`

> **Ollama vs llama.cpp 选择**：Ollama 更简单但内嵌的 llama.cpp 版本可能滞后，对新架构（如 `gemma4_unified`）支持不及时。需要精细控制参数或使用最新模型架构时选 llama.cpp。

## 2. 下载 GGUF 模型

```bash
# 先查仓库里有哪些量化文件
curl -sL "https://huggingface.co/api/models/<repo>" | python -c "
import sys,json
d=json.load(sys.stdin)
for s in d.get('siblings',[]):
    if '.gguf' in s['rfilename'].lower(): print(s['rfilename'])
"

# 下载指定量化（大文件用 nohup 后台跑）
huggingface-cli download <repo> <filename.gguf> --local-dir /e/models/
```

## 3. 量化版本选择与 VRAM 估算

选择量化前**必须**查显存：
```bash
nvidia-smi --query-gpu=name,memory.total,memory.free --format=csv,noheader
```

| 量化 | 典型大小 | 适合显存 |
|---|---|---|
| Q3_K_M | ~55% 原始 | 6-8 GB |
| Q4_K_M | ~65% 原始 | 8-12 GB |
| Q6_K | ~85% 原始 | 12-16 GB |
| Q8_0 | ~100% 原始 | 16-24 GB |

**上下文窗口估算公式**（关键，容易被忽略）：
```
剩余显存 = 空闲显存 - 模型权重大小
KV cache per token ≈ 2 × n_layers × n_kv_heads × head_dim × 2 bytes
最大 token 数 ≈ 剩余显存 / KV cache per token
```

对 Gemma 4 12B（48层, 16 KV heads, head_dim 128）：每 token ≈ 384 KB。
8 GB 显存 + Q3(5.7 GB) → 剩 ~2.3 GB → 最多 ~6000 tokens → `--ctx-size 4096` 保险。

> **重要**：`--ctx-size` 必须和 Navi config 中的 `context_window` 一致。先算再设，不要抄别人命令里的值。

## 4. 启动 llama-server

```bash
llama-server \
  -m E:/models/<model>.gguf \
  --alias <model-alias> \
  --ctx-size <calculated> \
  --n-gpu-layers 99 \
  -fa on \
  --jinja \
  --port 18080
```

参数说明：
- `--alias`：API 中的模型名，Navi config 里填这个名字
- `--jinja`：**必须**，否则工具调用的特殊 token 会泄漏
- `-fa on`：Flash Attention，省显存
- `--n-gpu-layers 99`：全部层放 GPU

启动后验证：`curl http://localhost:18080/health`

## 5. 接入 Navi

### 5.1 添加 Provider 类（router.py）

在 `navi_agent/model/router.py` 中注册新 provider：

```python
class LlamaProvider(BaseProvider):
    """llama.cpp / llama-server 等 OpenAI 兼容本地推理服务。"""
    def chat_stream_with_client(self, client, messages, tools, **kwargs):
        params: dict[str, Any] = dict(
            model=self.model_name, messages=messages, stream=True,
        )
        if tools:
            params["tools"] = tools
        return client.chat.completions.create(**params)

PROVIDER_CLASSES["llama"] = LlamaProvider
```

> 不要加 `stream_options`、`extra_body` 等参数——llama-server 不认。

### 5.2 配置 config.json

在 `~/.navi/config.json` 的 `providers` 中添加：

```json
"llama": {
    "api_key": "none",
    "base_url": "http://localhost:18080/v1",
    "models": {
        "<model-alias>": {
            "context_window": <与 --ctx-size 一致的值>
        }
    }
}
```

### 5.3 使用

Navi 中用 `/model` 切换到 `llama/<model-alias>`。

## 常见问题

- **输出 `0000...` 重复**：加 `--repeat-penalty 1.1`
- **泄漏 `<|tool_call>` 等原始 token**：确认 `--jinja` 已开启
- **OOM**：降量化（Q4→Q3）或减小 `--ctx-size`
- **`gemma4_unified` 架构加载失败**：llama.cpp 版本太旧，需更新
