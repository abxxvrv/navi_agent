from navi_agent.cli import init_command


def test_prompt_provider_config_accepts_lmstudio_model_id(monkeypatch):
    def prompt(text, default="", **kwargs):
        if text == "Select provider":
            return "lmstudio"
        if text.startswith("API key"):
            return ""
        if text == "Base URL":
            return default
        if text == "Model ID":
            return "local-model"
        if text == "Context window":
            return "24576"
        raise AssertionError(f"unexpected prompt: {text}")

    monkeypatch.setattr(init_command.typer, "prompt", prompt)
    monkeypatch.setattr(init_command.typer, "confirm", lambda *args, **kwargs: False)

    provider, model, provider_config = init_command._prompt_provider_config("Main model", {})

    assert provider == "lmstudio"
    assert model == "local-model"
    assert provider_config == {
        "api_key": "lm-studio",
        "base_url": "http://localhost:1234/v1",
        "models": {
            "local-model": {
                "context_window": 24576,
            }
        },
    }


def test_prompt_provider_config_marks_lmstudio_multimodal(monkeypatch):
    def prompt(text, default="", **kwargs):
        if text == "Select provider":
            return "lmstudio"
        if text.startswith("API key"):
            return "token"
        if text == "Base URL":
            return default
        if text == "Model ID":
            return "vision-model"
        if text == "Context window":
            return default
        raise AssertionError(f"unexpected prompt: {text}")

    monkeypatch.setattr(init_command.typer, "prompt", prompt)
    monkeypatch.setattr(init_command.typer, "confirm", lambda *args, **kwargs: True)

    _, _, provider_config = init_command._prompt_provider_config("Main model", {})

    assert provider_config["api_key"] == "token"
    assert provider_config["models"]["vision-model"] == {
        "context_window": 32768,
        "multimodal": True,
    }
