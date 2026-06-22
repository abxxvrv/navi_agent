---
name: llama-local-deploy
description: 本地部署 GGUF 模型并接入 Navi 作为 provider。当用户说"本地跑模型"、"部署到本地"、"用 llama.cpp 跑"、"接入 Navi"、"HuggingFace GGUF"、"本地推理"、"不想用 API"时使用。也适用于：给 Navi 添加本地模型 provider、配置 llama-server、估算量化模型的上下文窗口大小。触发词：本地部署、GGUF、llama.cpp、llama-server、本地模型、local model、quantize。
---

# 本地 GGUF 模型部署并接入 Navi

## 1. 安装 llama.cpp

下载 Vulkan 版预编译包（Windows）：

```bash
mkdir -p /e/llama.cpp
curl -L -o /e/llama.cpp/llama.zip "<release-url-vulkan-x64.zip>"
cd /e/llama.cpp && unzip -o llama.zip -d . && rm llama.zip
```

添加到用户 PATH（PowerShell）：

```powershell
$currentPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$newPath = "$currentPath;E:\llama.cpp"
[Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
```

## 2. 下载 GGUF 模型

```bash
pip install huggingface-hub  # 如未安装
huggingface-cli download <repo_id> <filename.gguf> --local-dir /e/models/
```

先用 API 查文件列表：

```bash
curl -sL "https://huggingface.co/api/models/<repo_id>" | python -c "
import sys,json
for s in json.load(sys.stdin).get('siblings',[]):
    if '.gguf' in s['rfilename'].lower(): print(s['rfilename'])
"
```

大文件（>2GB）用 nohup 后台下载，支持断点续传：

```bash
nohup huggingface-cli download ... > download.log 2>&1 &
```

## 3. 估算上下文窗口

**核心约束**：`context_window` 必须与 `--ctx-size` 一致，且不能超过显存允许的范围。

| 量化 | 模型权重 | 8GB 显存剩余 | 建议 ctx-size |
|------|---------|-------------|--------------|
| Q3_K_M (12B) | ~5.7 GB | ~2.3 GB | 4096 |
| Q4_K_M (12B) | ~6.9 GB | ~1.1 GB | 2048 |
| Q8_0 (12B) | ~11.8 GB | 需 16GB+ 卡 | 8192+ |

KV cache 每 token 约 384 KB（Gemma 4 12B），剩余显存 ÷ 384KB ≈ 可用 token 数。

## 4. 在 router.py 新增 LlamaProvider

文件：`navi_agent/model/router.py`

在 `MimoProvider` 之后、`PROVIDER_CLASSES` 之前插入：

```python
class LlamaProvider(BaseProvider):
    """llama.cpp / llama-server 等 OpenAI 兼容本地推理服务。"""

    def create_client(self) -> OpenAI:
        import httpx
        http_client = httpx.Client(trust_env=False)
        return OpenAI(api_key=self.api_key, base_url=self.base_url, http_client=http_client)

    def chat_stream_with_client(self, client, messages, tools, **kwargs):
        params: dict[str, Any] = dict(
            model=self.model_name, messages=messages, stream=True,
        )
        if tools:
            params["tools"] = tools
        return client.chat.completions.create(**params)
```

在 `PROVIDER_CLASSES` 字典中添加 `"llama": LlamaProvider`。

**关键**：`trust_env=False` 绕过系统代理。用户的 `HTTP_PROXY` 环境变量会导致 localhost 请求走代理返回 502，`proxy=None` 和 `proxies={}` 均无效，只有 `trust_env=False` 能解决。

## 5. 在 config.json 添加 provider

文件：`~/.navi/config.json` 的 `providers` 字段：

```json
"llama": {
    "api_key": "none",
    "base_url": "http://127.0.0.1:18080/v1",
    "models": {
        "<model-alias>": {
            "context_window": 4096
        }
    }
}
```

`model-alias` 需与 llama-server 的 `--alias` 参数一致。

## 6. 启动 llama-server

```bash
llama-server \
  -m /e/models/<model>.gguf \
  --alias <model-alias> \
  --ctx-size 4096 \
  --n-gpu-layers 99 \
  -fa on \
  --jinja \
  --port 18080
```

- `--jinja`：必须，否则工具调用特殊 token 会泄漏
- `-fa on`：Flash Attention，减少显存占用
- `--n-gpu-layers 99`：全部层放 GPU

## 7. 验证连接

绕过代理测试：

```bash
curl -s --noproxy '*' http://127.0.0.1:18080/v1/models
```

Python 测试（注意 trust_env=False）：

```python
from openai import OpenAI
import httpx
client = OpenAI(
    api_key='none',
    base_url='http://127.0.0.1:18080/v1',
    http_client=httpx.Client(trust_env=False),
)
resp = client.chat.completions.create(
    model='<alias>', messages=[{'role':'user','content':'hello'}],
    stream=False, max_tokens=50,
)
```

## 8. Navi 中使用

启动 Navi 后用 `/model` 切换到 `llama/<model-alias>`。
