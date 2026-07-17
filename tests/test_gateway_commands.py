from types import SimpleNamespace

import pytest

from navi_agent.gateway.commands import format_model_table, parse_gateway_command


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("/new", ("new", ())),
        ("/model list", ("model_list", ())),
        ("/model stepfun step-3.7-flash", ("model", ("stepfun", "step-3.7-flash"))),
        ("/model openrouter anthropic/claude-sonnet-4", ("model", ("openrouter", "anthropic/claude-sonnet-4"))),
    ],
)
def test_parse_gateway_command_accepts_exact_forms(text, expected):
    assert parse_gateway_command(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "/help",
        "/new now",
        "/model",
        "/model stepfun",
        "/model stepfun step-3.7-flash extra",
        "/model stepfun step-3.7-flash\nextra",
        "/Model stepfun step-3.7-flash",
    ],
)
def test_parse_gateway_command_rejects_other_slash_text(text):
    assert parse_gateway_command(text) is None


def test_format_model_table_has_provider_and_model_columns():
    models = {
        "stepfun": {"step-3.7-flash": {}},
        "deepseek": {"deepseek-chat": {}, "deepseek-reasoner": {}},
    }
    router = SimpleNamespace(
        list_providers=lambda: list(models),
        list_models=lambda provider: models[provider],
    )

    assert format_model_table(router) == (
        "| 提供商 | 模型名称 |\n"
        "| --- | --- |\n"
        "| stepfun | step-3.7-flash |\n"
        "| deepseek | deepseek-chat |\n"
        "| deepseek | deepseek-reasoner |"
    )
