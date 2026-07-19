from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

from .interrupt_scope import TurnScope


@dataclass(frozen=True)
class ToolExecutionContext:
    scope: TurnScope | None
    tool_call_id: str


CURRENT_TOOL_CONTEXT: ContextVar[ToolExecutionContext | None] = ContextVar(
    "navi_tool_execution_context",
    default=None,
)
