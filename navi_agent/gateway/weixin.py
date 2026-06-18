"""
WeChat (iLink) gateway adapter for Navi.

Long-polls the iLink ``getupdates`` endpoint and drives a per-chat
``AgentRuntime``. Each inbound text turn runs the synchronous, blocking
``runtime.run_turn`` inside ``asyncio.to_thread`` so the poll loop and typing
refresh keep running concurrently. Replies are formatted for WeChat and sent
in size-bounded chunks.

Text only — no media. Entry point: ``asyncio.run(WeixinAdapter(...).run())``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Optional

from ..paths import get_navi_home
from ..runtime.agent import AgentRuntime
from .ilink import (
    BACKOFF_DELAY_SECONDS,
    CRYPTO_AVAILABLE,
    ILINK_BASE_URL,
    LONG_POLL_TIMEOUT_MS,
    MAX_CONSECUTIVE_FAILURES,
    MESSAGE_DEDUP_TTL_SECONDS,
    RATE_LIMIT_ERRCODE,
    RETRY_DELAY_SECONDS,
    SESSION_EXPIRED_ERRCODE,
    TYPING_START,
    WEIXIN_CDN_BASE_URL,
    ContextTokenStore,
    MessageDeduplicator,
    TypingTicketCache,
    _extract_text,
    _get_config,
    _get_updates,
    _is_stale_session_ret,
    _load_sync_buf,
    _safe_id,
    _save_sync_buf,
    _send_message,
    _send_typing,
    _split_text_for_weixin_delivery,
    check_weixin_requirements,
    format_message,
    load_weixin_account,
    send_file,
)

logger = logging.getLogger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]

CANCEL_KEYWORD = "!cancel"


class WeixinAdapter:
    """Native Navi adapter for WeChat personal (iLink bot) accounts."""

    MAX_MESSAGE_LENGTH = 2000
    SEND_CHUNK_DELAY_SECONDS = 1.5
    TYPING_REFRESH_SECONDS = 10

    def __init__(
        self,
        account_id: str,
        *,
        workspace: str | Path,
        approval_mode: str = "open",
        base_url: Optional[str] = None,
    ):
        if not check_weixin_requirements():
            raise RuntimeError("aiohttp is required for the Weixin gateway (pip install aiohttp)")

        navi_home = str(get_navi_home())
        self._navi_home = navi_home
        self._account_id = str(account_id).strip()
        if not self._account_id:
            raise ValueError("account_id is required")

        creds = load_weixin_account(navi_home, self._account_id) or {}
        self._token = str(creds.get("token") or "").strip()
        if not self._token:
            raise RuntimeError(
                f"No saved credentials for account '{self._account_id}'. Run `navi weixin login` first."
            )
        self._base_url = str(base_url or creds.get("base_url") or ILINK_BASE_URL).strip().rstrip("/")

        self._workspace = str(workspace)
        self._approval_mode = approval_mode

        self._token_store = ContextTokenStore(navi_home)
        self._token_store.restore(self._account_id)
        self._typing_cache = TypingTicketCache()
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)

        self._runtimes: Dict[str, AgentRuntime] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

        self._poll_session: Optional["aiohttp.ClientSession"] = None
        self._send_session: Optional["aiohttp.ClientSession"] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        from .ilink import _make_ssl_connector

        self._poll_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        self._send_session = aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector())
        self._running = True
        logger.info("weixin: connected account=%s base=%s", _safe_id(self._account_id), self._base_url)
        try:
            await self._poll_loop()
        finally:
            self._running = False
            with contextlib.suppress(Exception):
                await self._poll_session.close()
            with contextlib.suppress(Exception):
                await self._send_session.close()

    async def _poll_loop(self) -> None:
        assert self._poll_session is not None
        sync_buf = _load_sync_buf(self._navi_home, self._account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0

        while self._running:
            try:
                response = await _get_updates(
                    self._poll_session,
                    base_url=self._base_url,
                    token=self._token,
                    sync_buf=sync_buf,
                    timeout_ms=timeout_ms,
                )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout

                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if (ret == SESSION_EXPIRED_ERRCODE or errcode == SESSION_EXPIRED_ERRCODE
                            or _is_stale_session_ret(ret, errcode, response.get("errmsg"))):
                        logger.error("weixin: session expired; pausing for 10 minutes")
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "weixin: getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)",
                        ret, errcode, response.get("errmsg", ""),
                        consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    await asyncio.sleep(
                        BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS
                    )
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        consecutive_failures = 0
                    continue

                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    _save_sync_buf(self._navi_home, self._account_id, sync_buf)

                for message in response.get("msgs") or []:
                    asyncio.create_task(self._handle_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error(
                    "weixin: poll error (%d/%d): %s",
                    consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc,
                )
                await asyncio.sleep(
                    BACKOFF_DELAY_SECONDS if consecutive_failures >= MAX_CONSECUTIVE_FAILURES else RETRY_DELAY_SECONDS
                )
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    consecutive_failures = 0

    # ── Inbound handling ──────────────────────────────────────────────────────

    async def _handle_message_safe(self, message: Dict[str, Any]) -> None:
        try:
            await self._handle_message(message)
        except Exception as exc:
            logger.error(
                "weixin: unhandled inbound error from=%s: %s",
                _safe_id(message.get("from_user_id")), exc, exc_info=True,
            )

    async def _handle_message(self, message: Dict[str, Any]) -> None:
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        text = _extract_text(message.get("item_list") or []).strip()
        if not text:
            return

        content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
        if self._dedup.is_duplicate(content_key):
            return

        chat_id = sender_id  # DM only

        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)

        # Handle cancellation *before* taking the per-chat lock — the lock is
        # held by the in-flight turn we want to interrupt.
        if text == CANCEL_KEYWORD:
            runtime = self._runtimes.get(chat_id)
            if runtime is not None:
                runtime.interrupt("用户通过微信请求取消")
                await self.send_text(chat_id, "已请求取消当前任务。")
            else:
                await self.send_text(chat_id, "当前没有正在运行的任务。")
            return

        # Fetch the typing ticket in the background so the first reply can show
        # the typing indicator once it is available.
        asyncio.create_task(self._fetch_typing_ticket(sender_id, context_token or None))

        async with self._chat_locks[chat_id]:
            runtime = self.get_or_create_runtime(chat_id)
            logger.info("weixin: inbound from=%s len=%d", _safe_id(sender_id), len(text))
            try:
                result = await self._run_with_typing(chat_id, runtime, text)
            except Exception as exc:
                logger.error("weixin: run_turn failed for %s: %s", _safe_id(chat_id), exc)
                await self.send_text(chat_id, f"处理消息时出错：{exc}")
                return

            answer = result.get("final_answer") or result.get("error") or "（本轮没有产生回复）"
            await self.send_text(chat_id, answer)
            for attach_path in result.get("pending_attachments") or []:
                try:
                    await send_file(
                        self._send_session,
                        base_url=self._base_url,
                        cdn_base_url=WEIXIN_CDN_BASE_URL,
                        token=self._token,
                        to=chat_id,
                        path=attach_path,
                        context_token=self._token_store.get(self._account_id, chat_id),
                        client_id=f"navi-weixin-{uuid.uuid4().hex}",
                    )
                except Exception as exc:
                    logger.warning("weixin: send_file failed for %s: %s", attach_path, exc)

    def get_or_create_runtime(self, chat_id: str) -> AgentRuntime:
        runtime = self._runtimes.get(chat_id)
        if runtime is None:
            runtime = AgentRuntime(
                workspace=self._workspace,
                approval_mode=self._approval_mode,
                on_output=None,
            )
            self._runtimes[chat_id] = runtime
        return runtime

    # ── Typing indicator ──────────────────────────────────────────────────────

    async def _run_with_typing(self, chat_id: str, runtime: AgentRuntime, text: str) -> Dict[str, Any]:
        typing_task = asyncio.create_task(self._keep_typing(chat_id))
        try:
            return await asyncio.to_thread(runtime.run_turn, text)
        finally:
            typing_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await typing_task

    async def _keep_typing(self, chat_id: str) -> None:
        try:
            while True:
                await self._send_typing(chat_id)
                await asyncio.sleep(self.TYPING_REFRESH_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _send_typing(self, chat_id: str) -> None:
        ticket = self._typing_cache.get(chat_id)
        if not ticket or self._send_session is None:
            return
        try:
            await _send_typing(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to_user_id=chat_id,
                typing_ticket=ticket,
                status=TYPING_START,
            )
        except Exception as exc:
            logger.debug("weixin: typing failed for %s: %s", _safe_id(chat_id), exc)

    async def _fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        if self._poll_session is None or self._typing_cache.get(user_id):
            return
        try:
            response = await _get_config(
                self._poll_session,
                base_url=self._base_url,
                token=self._token,
                user_id=user_id,
                context_token=context_token,
            )
            ticket = str(response.get("typing_ticket") or "")
            if ticket:
                self._typing_cache.set(user_id, ticket)
        except Exception as exc:
            logger.debug("weixin: getConfig failed for %s: %s", _safe_id(user_id), exc)

    # ── Outbound ──────────────────────────────────────────────────────────────

    async def send_text(self, chat_id: str, content: str) -> None:
        if not content or not content.strip() or self._send_session is None:
            return
        context_token = self._token_store.get(self._account_id, chat_id)
        chunks = [c for c in _split_text_for_weixin_delivery(format_message(content), self.MAX_MESSAGE_LENGTH) if c.strip()]
        for index, chunk in enumerate(chunks):
            await self._send_text_chunk(chat_id, chunk, context_token)
            if index < len(chunks) - 1:
                await asyncio.sleep(self.SEND_CHUNK_DELAY_SECONDS)

    async def _send_text_chunk(self, chat_id: str, chunk: str, context_token: Optional[str]) -> None:
        resp = await _send_message(
            self._send_session,
            base_url=self._base_url,
            token=self._token,
            to=chat_id,
            text=chunk,
            context_token=context_token,
            client_id=f"navi-weixin-{uuid.uuid4().hex}",
        )
        if not isinstance(resp, dict):
            return
        ret = resp.get("ret")
        errcode = resp.get("errcode")
        expired = (
            ret == SESSION_EXPIRED_ERRCODE
            or errcode == SESSION_EXPIRED_ERRCODE
            or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
        )
        # Session expired: drop the stale context_token and retry tokenless once.
        if expired and context_token:
            self._token_store.drop(self._account_id, chat_id)
            logger.warning("weixin: session expired for %s; retrying without context_token", _safe_id(chat_id))
            await _send_message(
                self._send_session,
                base_url=self._base_url,
                token=self._token,
                to=chat_id,
                text=chunk,
                context_token=None,
                client_id=f"navi-weixin-{uuid.uuid4().hex}",
            )
