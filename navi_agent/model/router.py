from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from openai import OpenAI


class BaseProvider(ABC):
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.api_key = api_key
        self.base_url = base_url
        self.client = self.create_client()
        self.model_name = model_name

    def create_client(self) -> OpenAI:
        return OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any:
        return self.chat_stream_with_client(self.client, messages, tools, **kwargs)

    @abstractmethod
    def chat_stream_with_client(
        self,
        client: OpenAI,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any: ...


class DeepSeekProvider(BaseProvider):
    def chat_stream_with_client(self, client, messages, tools, **kwargs):
        return client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            stream=True, stream_options={"include_usage": True},
            reasoning_effort="high", extra_body={"thinking": {"type": "enabled"}},
        )


class MimoProvider(BaseProvider):
    def chat_stream_with_client(self, client, messages, tools, **kwargs):
        return client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            stream=True, stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "enabled"}},
        )


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


PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
    "mimo": MimoProvider,
    "llama": LlamaProvider,
}


class ModelRouter:
    """模型路由器。

    config.json 存 providers 凭证、compression 配置和 default_model（仅新会话参考）。
    每个会话的实际模型由调用方传入 provider / model 参数。
    """

    def __init__(self, config_path: Path, provider: str, model: str):
        self.config_path = config_path
        self.config = self._load_config()
        self.provider = provider
        self.model = model
        self._provider = self._build_provider(self.provider, self.model)

        # 压缩模型路由（全局配置，与会话无关）
        compression_config = self.config.get("compression", {})
        self._compression_provider = self._build_provider(
            compression_config.get("provider", ""),
            compression_config.get("model", ""),
        )

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            except Exception:
                pass
        return {}

    def _build_provider(self, provider_name: str, model_name: str) -> BaseProvider | None:
        entry = self.config.get("providers", {}).get(provider_name)
        if not entry:
            return None
        provider_cls = PROVIDER_CLASSES.get(provider_name)
        if not provider_cls:
            return None
        return provider_cls(api_key=entry["api_key"], base_url=entry["base_url"], model_name=model_name)

    def chat_stream(self, messages, tools, **kwargs):
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.model}")
        return self._provider.chat_stream(messages, tools, **kwargs)

    def create_request_client(self) -> OpenAI:
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.model}")
        return self._provider.create_client()

    def chat_stream_with_client(self, client: OpenAI, messages, tools, **kwargs):
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.model}")
        return self._provider.chat_stream_with_client(client, messages, tools, **kwargs)

    def chat_stream_compression(self, messages, max_tokens=None, **kwargs):
        """压缩专用的 API 调用"""
        if self._compression_provider is None:
            raise RuntimeError("压缩模型未配置，请在 config.json 中设置 compression 字段")
        kwargs["max_tokens"] = max_tokens
        return self._compression_provider.chat_stream(messages, tools=[], **kwargs)

    @property
    def model_name(self) -> str:
        return self._provider.model_name if self._provider else self.model

    @property
    def context_window(self) -> int:
        models = self.config.get("providers", {}).get(self.provider, {}).get("models", {})
        return models.get(self.model, {}).get("context_window", 1048576)

    def list_providers(self) -> list[str]:
        return list(self.config.get("providers", {}).keys())

    def list_models(self, provider_name: str | None = None) -> dict:
        name = provider_name or self.provider
        return dict(self.config.get("providers", {}).get(name, {}).get("models", {}))

    def switch_model(self, provider_name: str, model_name: str) -> bool:
        if provider_name == self.provider and model_name == self.model:
            return True
        provider = self._build_provider(provider_name, model_name)
        if provider is None:
            return False
        self._provider = provider
        self.provider = provider_name
        self.model = model_name
        return True
