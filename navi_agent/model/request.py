from __future__ import annotations

import queue
import threading
from typing import Any

from ..runtime.interrupt import is_interrupted
from .router import ModelRouter


class ModelStreamRunner:
    """Runs a blocking model stream in a request worker thread.

    The caller stays in the runtime control thread and polls for chunks, so it
    can notice cancel_event even when the provider is blocked waiting for the
    next network chunk.
    """

    def __init__(
        self,
        router: ModelRouter,
        cancel_event: threading.Event,
        *,
        poll_interval: float = 0.25,
    ) -> None:
        self._router = router
        self._cancel_event = cancel_event
        self._poll_interval = poll_interval
        self._lock = threading.Lock()
        self._active_client: Any | None = None
        self._active_stream: Any | None = None

    def stream(self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        responses: queue.Queue[tuple[str, Any]] = queue.Queue()
        client = self._router.create_request_client()

        with self._lock:
            self._active_client = client

        def worker() -> None:
            try:
                stream = self._router.chat_stream_with_client(
                    client,
                    messages=messages,
                    tools=tools,
                )
                with self._lock:
                    if self._active_client is client:
                        self._active_stream = stream
                for chunk in stream:
                    responses.put(("chunk", chunk))
            except BaseException as exc:
                responses.put(("error", exc))
            finally:
                _close_resource(stream if "stream" in locals() else None)
                with self._lock:
                    if self._active_client is client:
                        self._active_stream = None
                responses.put(("done", None))

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()

        try:
            while True:
                if self._cancel_event.is_set() or is_interrupted():
                    self.abort()
                    raise KeyboardInterrupt("用户中断")

                try:
                    kind, payload = responses.get(timeout=self._poll_interval)
                except queue.Empty:
                    continue

                if kind == "chunk":
                    yield payload
                    continue
                if kind == "error":
                    if self._cancel_event.is_set() or is_interrupted():
                        raise KeyboardInterrupt("用户中断")
                    raise payload
                if kind == "done":
                    break
        finally:
            with self._lock:
                if self._active_client is client:
                    self._active_stream = None
                    self._active_client = None
            _close_client(client)

    def abort(self) -> None:
        with self._lock:
            client = self._active_client
            stream = self._active_stream
        _close_resource(stream)
        if client is not None:
            _close_client(client)


def _close_client(client: Any) -> None:
    _close_resource(client)


def _close_resource(client: Any) -> None:
    if client is None:
        return
    close = getattr(client, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        pass
