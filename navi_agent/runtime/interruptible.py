from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from .interrupt_scope import TurnScope


def run_model_stream(
    scope: TurnScope,
    runner: Any,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> Iterator[Any]:
    scope.raise_if_cancelled()
    with scope.aborter(runner.abort):
        for chunk in runner.stream(messages=messages, tools=tools):
            scope.raise_if_cancelled()
            yield chunk
    scope.raise_if_cancelled()


def wait_approval(
    scope: TurnScope,
    approval_handler: Callable[[Any], Any],
    decision: Any,
) -> Any:
    scope.raise_if_cancelled()
    cancel_approval = _approval_canceller_from_handler(approval_handler)
    with scope.approval_canceller(cancel_approval):
        choice = approval_handler(decision)
    scope.raise_if_cancelled()
    return choice


@contextmanager
def tool_worker(scope: TurnScope) -> Iterator[None]:
    with scope.tool_worker():
        yield


def _approval_canceller_from_handler(
    approval_handler: Callable[[Any], Any],
) -> Callable[[], None] | None:
    cancel = getattr(approval_handler, "cancel_current", None)
    if callable(cancel):
        return cancel

    owner = getattr(approval_handler, "__self__", None)
    cancel = getattr(owner, "cancel_current", None)
    if callable(cancel):
        return cancel
    return None
