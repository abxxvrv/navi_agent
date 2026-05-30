from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from openai import OpenAI

# 创建 OpenAI 客户端、存 model_name 和 max_tokens。定义了 chat() 抽象方法，子类必须实现。
class BaseProvider(ABC):
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        self.model_name = model_name

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        ...


class DeepSeekProvider(BaseProvider):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        return self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            tools=tools,
            reasoning_effort="high",
            extra_body={"thinking": {"type": "enabled"}},
        )


class MimoProvider(BaseProvider):
    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        return self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            tools=tools,
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
        self.current_name: str = self.config.get("current_model", "")
        self._provider = self._build_provider(self.current_name)
        self.last_usage: dict[str, int] = {}

    def _load_config(self) -> dict:
        if self.config_path.is_file():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _build_provider(self, name: str) -> BaseProvider | None:
        models = self.config.get("models", {})
        entry = models.get(name)
        if not entry:
            return None
        provider_cls = PROVIDER_CLASSES.get(entry.get("provider", name))
        if not provider_cls:
            return None
        return provider_cls(
            api_key=entry["api_key"],
            base_url=entry["base_url"],
            model_name=entry["model_name"],
        )

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **kwargs: Any,
    ) -> Any:
        if self._provider is None:
            raise RuntimeError(f"模型未配置或不可用: {self.current_name}")
        response = self._provider.chat(messages, tools, **kwargs)
        if hasattr(response, "usage") and response.usage:
            self.last_usage = {
                "prompt_tokens": response.usage.prompt_tokens or 0,
                "completion_tokens": response.usage.completion_tokens or 0,
                "total_tokens": response.usage.total_tokens or 0,
            }
        return response

    @property
    def model_name(self) -> str:
        if self._provider:
            return self._provider.model_name
        return self.current_name

    @property
    def context_window(self) -> int:
        models = self.config.get("models", {})
        entry = models.get(self.current_name, {})
        return entry.get("context_window", 1048576)

    def switch_model(self, name: str) -> bool:
        if name == self.current_name:
            return True
        provider = self._build_provider(name)
        if provider is None:
            return False
        self._provider = provider
        self.current_name = name
        self.config["current_model"] = name
        self.config_path.write_text(
            json.dumps(self.config, indent=2, ensure_ascii=False)
        )
        return True

    def list_models(self) -> dict[str, dict[str, str]]:
        result: dict[str, dict[str, str]] = {}
        for name, entry in self.config.get("models", {}).items():
            result[name] = {
                "provider": entry.get("provider", name),
                "model_name": entry.get("model_name", ""),
            }
        return result
