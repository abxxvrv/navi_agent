"""Exact slash commands shared by Navi gateway adapters."""

from __future__ import annotations

import re


_MODEL_COMMAND = re.compile(
    r"/model[ \t]+([^ \t\r\n]+)[ \t]+([^ \t\r\n]+)"
)


def parse_gateway_command(text: str) -> tuple[str, tuple[str, ...]] | None:
    if text == "/new":
        return "new", ()

    match = _MODEL_COMMAND.fullmatch(text)
    if match:
        return "model", match.groups()

    return None
