"""Exact slash commands shared by Navi gateway adapters."""

from __future__ import annotations

import re


_MODEL_COMMAND = re.compile(
    r"/model[ \t]+([^ \t\r\n]+)[ \t]+([^ \t\r\n]+)"
)


def parse_gateway_command(text: str) -> tuple[str, tuple[str, ...]] | None:
    if text == "/new":
        return "new", ()
    if text == "/model list":
        return "model_list", ()

    match = _MODEL_COMMAND.fullmatch(text)
    if match:
        return "model", match.groups()

    return None


def format_model_table(router) -> str:
    lines = ["| 提供商 | 模型名称 |", "| --- | --- |"]
    for provider in router.list_providers():
        for model in router.list_models(provider):
            lines.append(f"| {provider} | {model} |")
    return "\n".join(lines)
