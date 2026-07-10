import json

from navi_agent.model.router import LMStudioProvider, LongCatProvider, ModelRouter


def test_lmstudio_provider_uses_openai_compatible_params_with_tools():
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "stream"

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    provider = LMStudioProvider(
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        model_name="local-model",
    )
    tools = [{"type": "function", "function": {"name": "list_dir"}}]

    assert provider.chat_stream_with_client(FakeClient(), messages=[], tools=tools) == "stream"
    assert captured == {
        "model": "local-model",
        "messages": [],
        "stream": True,
        "tools": tools,
    }


def test_lmstudio_provider_omits_empty_tools():
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "stream"

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    provider = LMStudioProvider(
        api_key="lm-studio",
        base_url="http://localhost:1234/v1",
        model_name="local-model",
    )

    provider.chat_stream_with_client(FakeClient(), messages=[{"role": "user", "content": "hi"}], tools=[])

    assert captured == {
        "model": "local-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }


def test_model_router_builds_and_switches_lmstudio_provider(tmp_path):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "lmstudio": {
                        "api_key": "lm-studio",
                        "base_url": "http://localhost:1234/v1",
                        "models": {
                            "first": {"context_window": 32768},
                            "second": {"context_window": 24576},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    router = ModelRouter(config_path, provider="lmstudio", model="first")

    assert router.model_name == "first"
    assert router.context_window == 32768
    assert router.switch_model("lmstudio", "second") is True
    assert router.model_name == "second"
    assert router.context_window == 24576



def test_longcat_provider_uses_standard_openai_params_with_tools(monkeypatch):
    captured = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return "stream"

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(LongCatProvider, "create_client", lambda self: None)

    provider = LongCatProvider(
        api_key="longcat-key",
        base_url="https://api.longcat.chat/openai",
        model_name="LongCat-2.0",
    )
    tools = [{"type": "function", "function": {"name": "list_dir"}}]

    assert provider.chat_stream_with_client(FakeClient(), messages=[], tools=tools) == "stream"
    assert captured == {
        "model": "LongCat-2.0",
        "messages": [],
        "stream": True,
        "tools": tools,
    }


def test_model_router_builds_longcat_provider(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "providers": {
                    "longcat": {
                        "api_key": "longcat-key",
                        "base_url": "https://api.longcat.chat/openai",
                        "models": {
                            "LongCat-2.0": {"context_window": 1048576},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(LongCatProvider, "create_client", lambda self: None)

    router = ModelRouter(config_path, provider="longcat", model="LongCat-2.0")

    assert isinstance(router._provider, LongCatProvider)
    assert router.context_window == 1048576
