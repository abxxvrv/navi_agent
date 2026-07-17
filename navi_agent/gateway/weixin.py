"""
WeChat (iLink) gateway adapter for Navi.

Long-polls the iLink ``getupdates`` endpoint and drives a per-chat
``AgentRuntime``. Each inbound turn (text and/or media) runs the synchronous,
blocking ``runtime.run_turn`` inside ``asyncio.to_thread`` so the poll loop
and typing refresh keep running concurrently. Replies are formatted for WeChat
and sent in size-bounded chunks.

Inbound media (image / file / video / voice) is downloaded, decrypted, and
saved under ``<navi_home>/inbound/<account_id>/``; absolute paths are injected
into the agent's turn so it can read them directly.

Entry point: ``asyncio.run(WeixinAdapter(...).run())``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..paths import get_navi_home
from ..runtime.agent import AgentRuntime
from .commands import format_model_table, parse_gateway_command
from ..runtime.goal import parse_goal_command
from .ilink import (
    BACKOFF_DELAY_SECONDS,
    CRYPTO_AVAILABLE,
    ILINK_BASE_URL,
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_TEXT,
    ITEM_VIDEO,
    ITEM_VOICE,
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
    _atomic_json_write,
    _extract_text,
    _get_config,
    _get_updates,
    _is_stale_session_ret,
    _load_sync_buf,
    _safe_id,
    _send_message,
    _send_typing,
    _split_text_for_weixin_delivery,
    _sync_buf_path,
    download_inbound_media,
    format_message,
    load_allowlist,
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
        if aiohttp is None:
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
                    _atomic_json_write(
                        _sync_buf_path(self._navi_home, self._account_id),
                        {"get_updates_buf": sync_buf},
                    )

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

    # ── Inbound media helpers ─────────────────────────────────────────────────

    def _inbound_dir(self) -> Path:
        """Return (and create) the directory for inbound media files."""
        d = Path(self._navi_home) / "inbound" / self._account_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _safe_filename(self, name: str) -> str:
        """Sanitize a user-supplied filename, keeping only safe characters."""
        name = re.sub(r"[^\w.\-]", "_", name)
        name = re.sub(r"\.{2,}", ".", name)
        return name[:200] or "file"

    async def _collect_media(
        self, item_list: List[Dict[str, Any]]
    ) -> Tuple[List[Path], List[str]]:
        """Download inbound media items; return (image_paths, text_notes).

        - ITEM_IMAGE: downloaded, saved as <uuid>.jpg, appended to image_paths.
        - ITEM_FILE: downloaded with original filename (sanitized), note injected.
        - ITEM_VIDEO: downloaded as <uuid>.mp4, note injected.
        - ITEM_VOICE: if voice_item.text present, skipped (handled by _extract_text);
          otherwise downloaded as .silk, note injected.
        Single-item failures are logged as warnings and skipped.
        """
        image_paths: List[Path] = []
        notes: List[str] = []
        session = self._send_session  # reuse the send session for downloads

        for item in item_list:
            item_type = item.get("type")

            if item_type == ITEM_IMAGE:
                image_item = item.get("image_item") or {}
                media = image_item.get("media") or {}
                # Image aeskey may come as a raw hex string at image_item["aeskey"]
                raw_aeskey = image_item.get("aeskey")
                if raw_aeskey:
                    import base64 as _b64
                    aes_key_b64 = _b64.b64encode(bytes.fromhex(str(raw_aeskey))).decode("ascii")
                else:
                    aes_key_b64 = media.get("aes_key")
                try:
                    data = await download_inbound_media(
                        session,
                        cdn_base_url=WEIXIN_CDN_BASE_URL,
                        encrypt_query_param=media.get("encrypt_query_param"),
                        aes_key_b64=aes_key_b64,
                        full_url=media.get("full_url"),
                        timeout_seconds=30.0,
                    )
                    path = self._inbound_dir() / f"{uuid.uuid4().hex}.jpg"
                    path.write_bytes(data)
                    image_paths.append(path)
                except Exception as exc:
                    logger.warning("weixin: image download failed: %s", exc)

            elif item_type == ITEM_FILE:
                file_item = item.get("file_item") or {}
                media = file_item.get("media") or {}
                raw_name = self._safe_filename(str(file_item.get("file_name") or "file.bin"))
                filename = f"{uuid.uuid4().hex}_{raw_name}"
                try:
                    data = await download_inbound_media(
                        session,
                        cdn_base_url=WEIXIN_CDN_BASE_URL,
                        encrypt_query_param=media.get("encrypt_query_param"),
                        aes_key_b64=media.get("aes_key"),
                        full_url=media.get("full_url"),
                        timeout_seconds=60.0,
                    )
                    path = self._inbound_dir() / filename
                    path.write_bytes(data)
                    notes.append(f"[用户发来文件：{path}]")
                except Exception as exc:
                    logger.warning("weixin: file download failed: %s", exc)

            elif item_type == ITEM_VIDEO:
                video_item = item.get("video_item") or {}
                media = video_item.get("media") or {}
                filename = f"{uuid.uuid4().hex}.mp4"
                try:
                    data = await download_inbound_media(
                        session,
                        cdn_base_url=WEIXIN_CDN_BASE_URL,
                        encrypt_query_param=media.get("encrypt_query_param"),
                        aes_key_b64=media.get("aes_key"),
                        full_url=media.get("full_url"),
                        timeout_seconds=120.0,
                    )
                    path = self._inbound_dir() / filename
                    path.write_bytes(data)
                    notes.append(f"[用户发来视频：{path}]")
                except Exception as exc:
                    logger.warning("weixin: video download failed: %s", exc)

            elif item_type == ITEM_VOICE:
                voice_item = item.get("voice_item") or {}
                # If ASR text is present, _extract_text already handles it
                if voice_item.get("text"):
                    continue
                media = voice_item.get("media") or {}
                filename = f"{uuid.uuid4().hex}.silk"
                try:
                    data = await download_inbound_media(
                        session,
                        cdn_base_url=WEIXIN_CDN_BASE_URL,
                        encrypt_query_param=media.get("encrypt_query_param"),
                        aes_key_b64=media.get("aes_key"),
                        full_url=media.get("full_url"),
                        timeout_seconds=60.0,
                    )
                    path = self._inbound_dir() / filename
                    path.write_bytes(data)
                    notes.append(f"[用户发来语音文件：{path}]")
                except Exception as exc:
                    logger.warning("weixin: voice download failed: %s", exc)

        return image_paths, notes

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

        # 记录 context_token（回复未授权用户也需要它）
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)

        # 访问白名单：仅授权用户可驱动 bot。每条消息重新读盘，使 allow 立即生效。
        if sender_id not in load_allowlist(self._navi_home, self._account_id):
            await self._reject_unauthorized(sender_id)
            return

        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        item_list = message.get("item_list") or []
        text = _extract_text(item_list).strip()
        image_paths, notes = await self._collect_media(item_list)

        if not text and not image_paths and not notes:
            return

        # Content-hash dedup only when there is non-empty text (avoids colliding
        # distinct media-only messages that both have empty text).
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                return

        # Compose the turn text: path notes first, then the user's text
        message_text = "\n".join(notes + ([text] if text else []))

        chat_id = sender_id  # DM only

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

        goal_command = (
            parse_goal_command(text)
            if all(item.get("type") == ITEM_TEXT for item in item_list)
            else None
        )
        if goal_command is not None and goal_command[0] in {"pause", "cancel"}:
            runtime = self._runtimes.get(chat_id)
            if runtime is None:
                await self.send_text(chat_id, "No active or resumable goal.")
                return
            command_result = runtime.goal_runner.apply_command(*goal_command)
            if command_result["ok"]:
                runtime.interrupt(f"用户通过微信请求 {goal_command[0]} goal")
            await self.send_text(chat_id, command_result["message"])
            return

        command = (
            parse_gateway_command(text)
            if all(item.get("type") == ITEM_TEXT for item in item_list)
            else None
        )
        if command:
            async with self._chat_locks[chat_id]:
                command_name, command_args = command
                if command_name == "new":
                    self._runtimes.pop(chat_id, None)
                    self.get_or_create_runtime(chat_id)
                    await self.send_text(chat_id, "已开启新对话。")
                    return

                runtime = self.get_or_create_runtime(chat_id)
                if command_name == "model_list":
                    await self.send_text(chat_id, format_model_table(runtime.router))
                    return

                provider, model = command_args
                if runtime.switch_model(provider, model):
                    await self.send_text(chat_id, f"已切换模型：{provider}/{model}")
                else:
                    await self.send_text(chat_id, f"模型切换失败：{provider}/{model}")
                return

        # Fetch the typing ticket in the background so the first reply can show
        # the typing indicator once it is available.
        asyncio.create_task(self._fetch_typing_ticket(sender_id, context_token or None))

        async with self._chat_locks[chat_id]:
            runtime = self.get_or_create_runtime(chat_id)
            if goal_command is not None:
                command_result = runtime.goal_runner.apply_command(*goal_command)
                if command_result["run_input"] is None:
                    await self.send_text(chat_id, command_result["message"])
                    return
                message_text = command_result["run_input"]
            logger.info(
                "weixin: inbound from=%s text_len=%d images=%d notes=%d",
                _safe_id(sender_id), len(message_text), len(image_paths), len(notes),
            )
            try:
                result = await self._run_with_typing(chat_id, runtime, message_text, image_paths)
            except Exception as exc:
                logger.error("weixin: run_turn failed for %s: %s", _safe_id(chat_id), exc)
                await self.send_text(chat_id, f"处理消息时出错：{exc}")
                return

            answer = result.get("final_answer") or result.get("error") or "（本轮没有产生回复）"
            usage = runtime.last_usage
            window = runtime.router.context_window
            pct = round((usage.get("prompt_tokens", 0) if usage else 0) / window * 100) if window else 0
            workspace = Path(self._workspace).expanduser().resolve()
            home = Path.home().resolve()
            if workspace == home:
                workspace_text = "~"
            elif workspace.is_relative_to(home):
                workspace_text = "~/" + workspace.relative_to(home).as_posix()
            else:
                workspace_text = workspace.as_posix()
            model_name = result.get("model_name") or runtime.router.model_name
            answer = f"{answer.rstrip()}\n\n{model_name} · {pct}% · {workspace_text}"
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
                channel="weixin",
            )
            self._runtimes[chat_id] = runtime
        return runtime

    async def _reject_unauthorized(self, sender_id: str) -> None:
        # 限流：同一未授权用户在 dedup TTL 内只提示一次，避免被刷消息放大
        if self._dedup.is_duplicate(f"unauth-notify:{sender_id}"):
            return
        logger.warning("weixin: 拒绝未授权用户 %s", _safe_id(sender_id))
        await self.send_text(
            sender_id,
            "⚠️ 未授权访问。\n"
            f"你的用户 ID：{sender_id}\n"
            "如需使用，请在运行 Navi 的机器上执行：\n"
            f"navi weixin allow {sender_id} --account {self._account_id}",
        )

    # ── Typing indicator ──────────────────────────────────────────────────────

    async def _run_with_typing(
        self,
        chat_id: str,
        runtime: AgentRuntime,
        text: str,
        image_paths: Optional[List[Path]] = None,
    ) -> Dict[str, Any]:
        typing_task = asyncio.create_task(self._keep_typing(chat_id))
        try:
            return await asyncio.to_thread(runtime.goal_runner.drive, text, image_paths)
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
