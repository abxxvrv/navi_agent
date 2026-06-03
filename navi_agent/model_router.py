from __future__ import annotations

import json
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from openai import OpenAI, RateLimitError


class BaseProvider(ABC):
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model_name = model_name

    @abstractmethod
    def chat(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any: ...
    @abstractmethod
    def chat_stream(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **kwargs: Any) -> Any: ...


class DeepSeekProvider(BaseProvider):
    def chat(self, messages, tools, **kwargs):
        return self.client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            reasoning_effort="high", extra_body={"thinking": {"type": "enabled"}},
        )
    def chat_stream(self, messages, tools, **kwargs):
        return self.client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            stream=True, stream_options={"include_usage": True},
            reasoning_effort="high", extra_body={"thinking": {"type": "enabled"}},
        )


class MimoProvider(BaseProvider):
    def chat(self, messages, tools, **kwargs):
        return self.client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            extra_body={"thinking": {"type": "enabled"}},
        )
    def chat_stream(self, messages, tools, **kwargs):
        return self.client.chat.completions.create(
            model=self.model_name, messages=messages, tools=tools,
            stream=True, stream_options={"include_usage": True},
            extra_body={"thinking": {"type": "enabled"}},
        )


PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "deepseek": DeepSeekProvider,
    "mimo": MimoProvider,
}


class ModelRouter:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = self._load_config()
        self.current_provider: str = self.config.get("current_provider", "")
        self.current_model: str = self.config.get("current_model", "")
        self._provider = self._build_provider(self.current_provider, self.current_model)

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
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

    def chat(self, messages, tools, max_retries: int = 5, **kwargs):
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.current_model}")
        for attempt in range(max_retries):
            try:
                return self._provider.chat(messages, tools, **kwargs)
            except RateLimitError:
                if attempt == max_retries - 1:
                    raise
                time.sleep(2 ** (attempt + 1))
        raise RuntimeError("unreachable")

    def chat_stream(self, messages, tools, **kwargs):
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.current_model}")
        return self._provider.chat_stream(messages, tools, **kwargs)

    @property
    def model_name(self) -> str:
        return self._provider.model_name if self._provider else self.current_model

    @property
    def context_window(self) -> int:
        models = self.config.get("providers", {}).get(self.current_provider, {}).get("models", {})
        return models.get(self.current_model, {}).get("context_window", 1048576)

    def list_providers(self) -> list[str]:
        return list(self.config.get("providers", {}).keys())

    def list_models(self, provider_name: str | None = None) -> dict:
        name = provider_name or self.current_provider
        return dict(self.config.get("providers", {}).get(name, {}).get("models", {}))

    def switch_model(self, provider_name: str, model_name: str) -> bool:
        if provider_name == self.current_provider and model_name == self.current_model:
            return True
        provider = self._build_provider(provider_name, model_name)
        if provider is None:
            return False
        self._provider = provider
        self.current_provider = provider_name
        self.current_model = model_name
        self.config["current_provider"] = provider_name
        self.config["current_model"] = model_name
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))
        return True
