"""
QQ Bot gateway adapter for Navi.

Connects to the QQ official open-platform WebSocket gateway, drives a per-chat
``AgentRuntime`` for each authorized C2C (private) conversation, and sends
replies as size-bounded passive messages. This mirrors the WeChat gateway's
DM-only experience; only the transport differs (WebSocket gateway + REST API
instead of iLink long-poll).

Inbound attachments (image / file / video / voice) are downloaded and saved
under ``<navi_home>/inbound/qq/<account_id>/``; absolute paths are injected into
the agent's turn so it can read them directly.

Entry point: ``asyncio.run(QqAdapter(...).run())``.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..paths import get_navi_home
from ..runtime.agent import AgentRuntime
from .ilink import (
    MessageDeduplicator,
    _safe_id,
    _split_text_for_weixin_delivery,
    format_message,
)
from . import qqbot
from .qqbot import (
    CONNECT_TIMEOUT_SECONDS,
    INTENT_GROUP_AND_C2C,
    MAX_MESSAGE_LENGTH,
    MAX_RECONNECT_ATTEMPTS,
    MESSAGE_DEDUP_TTL_SECONDS,
    OP_DISPATCH,
    OP_HEARTBEAT,
    OP_HEARTBEAT_ACK,
    OP_HELLO,
    OP_IDENTIFY,
    OP_INVALID_SESSION,
    OP_RECONNECT,
    OP_RESUME,
    RECONNECT_BACKOFF,
    build_user_agent,
    download_inbound_media,
    load_qq_account,
    load_qq_allowlist,
    send_c2c_typing,
    send_media_file,
    send_message_text,
)

logger = logging.getLogger(__name__)

try:
    import aiohttp
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]

CANCEL_KEYWORD = "!cancel"


def _looks_like_image(data: bytes) -> bool:
    """Return True if data starts with a known image magic-byte sequence."""
    if len(data) < 4:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:3] == b"\xff\xd8\xff":
        return True
    if data[:6] in {b"GIF87a", b"GIF89a"}:
        return True
    if data[:2] == b"BM":
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


class QqCloseError(Exception):
    """Raised when the gateway WebSocket is closed by the server."""

    def __init__(self, code: Any, reason: str = ""):
        super().__init__(f"WebSocket closed: code={code} reason={reason}")
        self.code = code
        self.reason = reason


class QqAdapter:
    """Native Navi adapter for QQ official bot (C2C / private) accounts."""

    SEND_CHUNK_DELAY_SECONDS = 1.0
    TYPING_INPUT_SECONDS = 30
    # Fatal gateway close codes — stop reconnecting (bot misconfiguration / ban).
    FATAL_CLOSE_CODES = {4001, 4002, 4010, 4011, 4012, 4013, 4014, 4914, 4915}

    def __init__(
        self,
        account_id: str,
        *,
        workspace: str | Path,
        approval_mode: str = "open",
    ):
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for the QQ gateway (pip install aiohttp)")

        navi_home = str(get_navi_home())
        self._navi_home = navi_home
        self._account_id = str(account_id).strip()
        if not self._account_id:
            raise ValueError("account_id is required")

        creds = load_qq_account(navi_home, self._account_id) or {}
        self._app_id = str(creds.get("app_id") or "").strip()
        self._client_secret = str(creds.get("client_secret") or "").strip()
        if not self._app_id or not self._client_secret:
            raise RuntimeError(
                f"No saved credentials for account '{self._account_id}'. Run `navi qq login` first."
            )

        self._workspace = str(workspace)
        self._approval_mode = approval_mode

        # QQ 原生 markdown 渲染，默认开；若账号无 markdown 权限，首条被拒后
        # 自动降级为纯文本，之后本实例都走纯文本。
        self._markdown_enabled = True

        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)
        self._runtimes: Dict[str, AgentRuntime] = {}
        self._chat_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._chat_type: Dict[str, str] = {}  # chat_id → "c2c" | "group"

        # Passive-reply context per chat (set inside the per-chat lock before a turn).
        self._reply_msg_id: Dict[str, str] = {}
        self._reply_seq: Dict[str, int] = {}

        # Token cache
        self._token: Optional[str] = None
        self._token_expires_at: float = 0.0

        # Connection state
        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._heartbeat_interval: float = 30.0
        self._session_id: Optional[str] = None
        self._last_seq: Optional[int] = None
        self._running = False

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def run(self) -> None:
        self._session = aiohttp.ClientSession(trust_env=True)
        self._running = True
        logger.info("qq: starting account=%s", _safe_id(self._account_id))
        try:
            await self._listen_loop()
        finally:
            self._running = False
            await self._close_ws()
            with contextlib.suppress(Exception):
                await self._session.close()

    async def _ensure_token(self) -> str:
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token
        data = await qqbot.get_access_token(
            self._session, app_id=self._app_id, client_secret=self._client_secret
        )
        self._token = str(data["access_token"])
        self._token_expires_at = time.time() + int(data.get("expires_in", 7200))
        logger.info("qq: access token refreshed")
        return self._token

    async def _open_ws(self) -> None:
        token = await self._ensure_token()
        gateway_url = await qqbot.get_gateway_url(self._session, token=token)
        ws_proxy = (
            os.getenv("WSS_PROXY")
            or os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )
        await self._close_ws()
        self._ws = await self._session.ws_connect(
            gateway_url,
            headers={"User-Agent": build_user_agent()},
            timeout=CONNECT_TIMEOUT_SECONDS,
            proxy=ws_proxy,
        )
        logger.info("qq: connected to gateway %s", gateway_url)

    async def _close_ws(self) -> None:
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._heartbeat_task
            self._heartbeat_task = None
        if self._ws and not self._ws.closed:
            with contextlib.suppress(Exception):
                await self._ws.close()
        self._ws = None

    async def _listen_loop(self) -> None:
        backoff_idx = 0
        while self._running:
            try:
                await self._open_ws()
                self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except QqCloseError as exc:
                logger.warning("qq: %s", exc)
                if exc.code in self.FATAL_CLOSE_CODES:
                    logger.error("qq: fatal gateway code %s — check QQ Open Platform config", exc.code)
                    return
                if exc.code in {4006, 4007, 4009}:
                    self._session_id = None
                    self._last_seq = None
                backoff_idx = await self._backoff(backoff_idx)
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("qq: connection error: %s", exc)
                backoff_idx = await self._backoff(backoff_idx)

    async def _backoff(self, backoff_idx: int) -> int:
        if backoff_idx >= MAX_RECONNECT_ATTEMPTS:
            logger.error("qq: max reconnect attempts reached")
            self._running = False
            return backoff_idx
        delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
        logger.info("qq: reconnecting in %ds (attempt %d)", delay, backoff_idx + 1)
        await asyncio.sleep(delay)
        return backoff_idx + 1

    async def _read_events(self) -> None:
        assert self._ws is not None
        while self._running and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    payload = json.loads(msg.data)
                except Exception:
                    logger.warning("qq: failed to parse frame: %r", msg.data[:200])
                    continue
                if isinstance(payload, dict):
                    self._dispatch(payload)
            elif msg.type == aiohttp.WSMsgType.CLOSE:
                raise QqCloseError(msg.data, str(msg.extra or ""))
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR}:
                raise QqCloseError("closed", "transport closed")

    async def _heartbeat_loop(self) -> None:
        try:
            while self._running and self._ws and not self._ws.closed:
                await asyncio.sleep(self._heartbeat_interval)
                with contextlib.suppress(Exception):
                    await self._ws.send_json({"op": OP_HEARTBEAT, "d": self._last_seq})
        except asyncio.CancelledError:
            pass

    # ── Gateway dispatch ──────────────────────────────────────────────────────

    def _dispatch(self, payload: Dict[str, Any]) -> None:
        op = payload.get("op")
        seq = payload.get("s")
        if isinstance(seq, int) and (self._last_seq is None or seq > self._last_seq):
            self._last_seq = seq

        if op == OP_HELLO:
            interval_ms = (payload.get("d") or {}).get("heartbeat_interval", 30000)
            self._heartbeat_interval = interval_ms / 1000.0 * 0.8
            if self._session_id and self._last_seq is not None:
                asyncio.create_task(self._send_resume())
            else:
                asyncio.create_task(self._send_identify())
            return

        if op == OP_DISPATCH:
            event_type = payload.get("t")
            d = payload.get("d")
            if event_type == "READY":
                self._session_id = (d or {}).get("session_id")
                logger.info("qq: ready, session_id=%s", _safe_id(self._session_id))
            elif event_type == "RESUMED":
                logger.info("qq: session resumed")
            elif event_type == "C2C_MESSAGE_CREATE":
                asyncio.create_task(self._handle_message_safe(d, "c2c"))
            elif event_type == "GROUP_AT_MESSAGE_CREATE":
                asyncio.create_task(self._handle_message_safe(d, "group"))
            else:
                logger.debug("qq: unhandled dispatch %s", event_type)
            return

        if op == OP_HEARTBEAT_ACK:
            return

        if op in {OP_RECONNECT, OP_INVALID_SESSION}:
            if op == OP_INVALID_SESSION and not bool(payload.get("d")):
                self._session_id = None
                self._last_seq = None
            if self._ws and not self._ws.closed:
                asyncio.create_task(self._ws.close())

    async def _send_identify(self) -> None:
        token = await self._ensure_token()
        with contextlib.suppress(Exception):
            await self._ws.send_json(
                {
                    "op": OP_IDENTIFY,
                    "d": {
                        "token": f"QQBot {token}",
                        "intents": INTENT_GROUP_AND_C2C,
                        "shard": [0, 1],
                        "properties": {"$os": "navi", "$browser": "navi", "$device": "navi"},
                    },
                }
            )
            logger.info("qq: identify sent")

    async def _send_resume(self) -> None:
        token = await self._ensure_token()
        try:
            await self._ws.send_json(
                {
                    "op": OP_RESUME,
                    "d": {
                        "token": f"QQBot {token}",
                        "session_id": self._session_id,
                        "seq": self._last_seq,
                    },
                }
            )
            logger.info("qq: resume sent")
        except Exception:
            self._session_id = None
            self._last_seq = None

    # ── Inbound media ─────────────────────────────────────────────────────────

    def _inbound_dir(self) -> Path:
        d = Path(self._navi_home) / "inbound" / "qq" / self._account_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _safe_filename(self, name: str) -> str:
        # 去掉路径分隔符、Windows 非法字符和控制字符，保留中文等正常字符。
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
        name = re.sub(r"\.{2,}", ".", name)
        return name[:200] or "file"

    def _extract_atme_media(self, message: Dict[str, Any]) -> List[Dict[str, str]]:
        """从群 @ 事件的 msg_elements 里，抽取【@ 发起者本人】发的图片/文件，
        转成 _collect_media 认识的附件 dict（动图/表情、他人发的媒体一律跳过）。

        群消息没有 attachments 字段，QQ 把"最近N条"渲染成文本放进 msg_elements，
        形如：
            === 消息 1 ===
            [发送者] 名字
            [附件1] 类型:图片 文件名:X 尺寸:.. 大小:.. URL:https://...
        """
        elements = message.get("msg_elements")
        if not isinstance(elements, list):
            return []
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        sender_name = str(author.get("username") or "").strip()
        if not sender_name:
            return []
        text = "\n".join(
            str(el.get("content") or "") for el in elements if isinstance(el, dict)
        )
        out: List[Dict[str, str]] = []
        for block in re.split(r"===\s*消息\s*\d+\s*===", text):
            m = re.search(r"\[发送者\]\s*(.+)", block)
            if not m or m.group(1).strip() != sender_name:
                continue  # 只处理 @ 发起者本人发的媒体
            for line in block.splitlines():
                am = re.match(
                    r"\s*\[附件\d+\]\s*类型:(\S+)\s+文件名:(\S+).*?URL:(\S+)", line
                )
                if not am:
                    continue
                kind, fname, url = am.group(1), am.group(2), am.group(3).strip()
                if kind == "图片":
                    out.append({"url": url, "content_type": "image", "filename": fname})
                elif kind == "文件":
                    out.append({"url": url, "content_type": "", "filename": fname})
                # 动图/表情等其它类型：跳过
        return out

    async def _collect_media(
        self, attachments: List[Dict[str, Any]]
    ) -> Tuple[List[Path], List[str]]:
        """Download inbound attachments; return (image_paths, text_notes).

        Images are appended to image_paths (passed to the agent as vision input);
        other media types are saved and surfaced to the agent as path notes.
        """
        image_paths: List[Path] = []
        notes: List[str] = []
        token = await self._ensure_token()
        for att in attachments:
            if not isinstance(att, dict):
                continue
            url = str(att.get("url") or "").strip()
            if not url:
                continue
            content_type = str(att.get("content_type") or "").lower()
            raw_name = self._safe_filename(str(att.get("filename") or ""))

            # 诊断日志：入站附件是否到达、来自哪个主机、什么类型（排查文件收不到）。
            logger.info(
                "qq: inbound attachment content_type=%r host=%s name=%r",
                content_type, urlparse(url).hostname or "?", raw_name,
            )

            # 语音优先：如果平台已提供 asr_refer_text，直接使用，跳过下载
            if content_type.startswith("audio") or content_type.startswith("voice") or raw_name.lower().endswith(
                (".silk", ".amr", ".mp3", ".wav", ".ogg", ".m4a", ".aac", ".speex", ".flac")
            ):
                asr_text = str(att.get("asr_refer_text") or "").strip()
                if asr_text:
                    notes.append(f"[Voice] {asr_text}")
                    continue

            try:
                data = await download_inbound_media(
                    self._session, url=url, timeout_seconds=60.0, token=token
                )
            except Exception as exc:
                logger.warning("qq: attachment download failed: %s", exc)
                continue

            if content_type.startswith("image") or raw_name.lower().endswith(
                (".jpg", ".jpeg", ".png", ".gif", ".webp")
            ):
                # 校验 magic bytes，拒绝非图片数据
                if not _looks_like_image(data):
                    logger.warning("qq: rejecting non-image attachment from %s", url[:80])
                    continue
                path = self._inbound_dir() / f"{uuid.uuid4().hex}.jpg"
                path.write_bytes(data)
                image_paths.append(path)
            else:
                filename = f"{uuid.uuid4().hex}_{raw_name}" if raw_name else f"{uuid.uuid4().hex}.bin"
                path = self._inbound_dir() / filename
                path.write_bytes(data)
                if content_type.startswith("video"):
                    notes.append(f"[用户发来视频：{path}]")
                elif content_type.startswith("audio") or content_type.startswith("voice"):
                    notes.append(f"[用户发来语音文件：{path}]")
                else:
                    notes.append(f"[用户发来文件：{path}]")
        return image_paths, notes

    # ── Inbound handling ──────────────────────────────────────────────────────

    async def _handle_message_safe(self, message: Any, chat_type: str) -> None:
        try:
            await self._handle_message(message, chat_type)
        except Exception as exc:
            logger.error("qq: unhandled inbound error: %s", exc, exc_info=True)

    async def _handle_message(self, message: Any, chat_type: str) -> None:
        if not isinstance(message, dict):
            return
        author = message.get("author") if isinstance(message.get("author"), dict) else {}
        message_id = str(message.get("id") or "").strip()

        # Resolve chat_id (the reply target) and sender by scene, then apply the
        # matching fail-closed allowlist. Re-read on every message so `allow`
        # takes effect live.
        if chat_type == "group":
            chat_id = str(message.get("group_openid") or "").strip()
            sender_id = str(author.get("member_openid") or "").strip()
            if not chat_id:
                return
            # Group allowlist keys on the group openid. QQ openids are encrypted
            # per-bot and can't be derived from a group number — the only way to
            # learn a group's openid is to observe its message. So instead of a
            # silent drop, log the openid (throttled) so it can be authorized;
            # we never post a rejection notice into the group itself.
            if chat_id not in load_qq_allowlist(self._navi_home, self._account_id, "group"):
                if not self._dedup.is_duplicate(f"unauth-group:{chat_id}"):
                    logger.warning(
                        "qq: 收到未授权群消息，group_openid=%s。如需启用，请执行 "
                        "`navi qq allow %s --group --account %s`",
                        chat_id, chat_id, self._account_id,
                    )
                return
        else:
            chat_id = str(author.get("user_openid") or "").strip()
            sender_id = chat_id
            if not chat_id:
                return
            if chat_id not in load_qq_allowlist(self._navi_home, self._account_id):
                await self._reject_unauthorized(chat_id, message_id)
                return

        # Remember the scene so outbound helpers pick the right REST path. Set
        # before the lock so a !cancel reply also routes correctly.
        self._chat_type[chat_id] = chat_type

        if message_id and self._dedup.is_duplicate(message_id):
            return

        text = str(message.get("content") or "").strip()
        if chat_type == "group":
            # Strip a leading @bot mention (e.g. "<@!123> foo") the platform keeps.
            text = re.sub(r"^<@!?\d+>\s*", "", text).strip()
        attachments = list(message.get("attachments") or [])
        if chat_type == "group":
            # 群消息的图片/文件在 msg_elements 里（仅取 @ 发起者本人发的）。
            attachments += self._extract_atme_media(message)
        image_paths, notes = await self._collect_media(attachments)
        if not text and not image_paths and not notes:
            return

        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                return

        message_text = "\n".join(notes + ([text] if text else []))

        # Cancellation before taking the lock — it is held by the in-flight turn.
        if text == CANCEL_KEYWORD:
            runtime = self._runtimes.get(chat_id)
            if runtime is not None:
                runtime.interrupt("用户通过 QQ 请求取消")
                await self.send_text(chat_id, "已请求取消当前任务。", message_id)
            else:
                await self.send_text(chat_id, "当前没有正在运行的任务。", message_id)
            return

        async with self._chat_locks[chat_id]:
            # Bind the passive-reply context for this turn (msg_id + seq counter).
            self._reply_msg_id[chat_id] = message_id
            self._reply_seq[chat_id] = 0

            runtime = self.get_or_create_runtime(chat_id)
            logger.info(
                "qq: inbound type=%s from=%s chat=%s text_len=%d images=%d notes=%d",
                chat_type, _safe_id(sender_id), _safe_id(chat_id),
                len(message_text), len(image_paths), len(notes),
            )
            # Typing (input_notify) is only supported for C2C.
            if chat_type == "c2c":
                await self._send_typing(chat_id, message_id)
            try:
                result = await asyncio.to_thread(runtime.run_turn, message_text, image_paths)
            except Exception as exc:
                logger.error("qq: run_turn failed for %s: %s", _safe_id(chat_id), exc)
                await self.send_text(chat_id, f"处理消息时出错：{exc}", message_id)
                return

            answer = result.get("final_answer") or result.get("error") or "（本轮没有产生回复）"
            await self.send_text(chat_id, answer, message_id)
            for attach_path in result.get("pending_attachments") or []:
                await self._send_attachment(chat_id, attach_path, message_id)

    def get_or_create_runtime(self, chat_id: str) -> AgentRuntime:
        runtime = self._runtimes.get(chat_id)
        if runtime is None:
            runtime = AgentRuntime(
                workspace=self._workspace,
                approval_mode=self._approval_mode,
                on_output=None,
                channel="qq",
            )
            self._runtimes[chat_id] = runtime
        return runtime

    async def _reject_unauthorized(self, sender_id: str, msg_id: Any) -> None:
        if self._dedup.is_duplicate(f"unauth-notify:{sender_id}"):
            return
        logger.warning("qq: 拒绝未授权用户 %s", _safe_id(sender_id))
        await self.send_text(
            sender_id,
            "⚠️ 未授权访问。\n"
            f"你的用户 ID：{sender_id}\n"
            "如需使用，请在运行 Navi 的机器上执行：\n"
            f"navi qq allow {sender_id} --account {self._account_id}",
            str(msg_id or "").strip() or None,
        )

    # ── Outbound ──────────────────────────────────────────────────────────────

    def _next_seq(self, chat_id: str) -> int:
        seq = self._reply_seq.get(chat_id, 0) + 1
        self._reply_seq[chat_id] = seq
        return seq

    async def _send_typing(self, chat_id: str, msg_id: Optional[str]) -> None:
        if not msg_id:
            return
        try:
            token = await self._ensure_token()
            await send_c2c_typing(
                self._session,
                token=token,
                openid=chat_id,
                msg_id=msg_id,
                msg_seq=self._next_seq(chat_id),
                input_seconds=self.TYPING_INPUT_SECONDS,
            )
        except Exception as exc:
            logger.debug("qq: typing failed for %s: %s", _safe_id(chat_id), exc)

    async def send_text(self, chat_id: str, content: str, msg_id: Optional[str]) -> None:
        if not content or not content.strip():
            return
        chat_type = self._chat_type.get(chat_id, "c2c")
        # 默认发 QQ 原生 markdown（客户端渲染）。首条被拒（多为无 markdown 权限）
        # 时全局降级并把整条消息改用纯文本重发一次。
        markdown = self._markdown_enabled
        while True:
            if markdown:
                # markdown 只按块切分，不套 format_message（其折行会破坏 markdown 结构）。
                source = content
            else:
                source = format_message(content)
            chunks = [
                c
                for c in _split_text_for_weixin_delivery(source, MAX_MESSAGE_LENGTH)
                if c.strip()
            ]
            fell_back = False
            for index, chunk in enumerate(chunks):
                try:
                    token = await self._ensure_token()
                    await send_message_text(
                        self._session,
                        token=token,
                        chat_type=chat_type,
                        target_id=chat_id,
                        text=chunk,
                        msg_id=msg_id,
                        msg_seq=self._next_seq(chat_id),
                        markdown=markdown,
                    )
                except Exception as exc:
                    # 只有 API 明确返回错误（RuntimeError，来自 _api_request 的 HTTP 4xx，
                    # 即账号无 markdown 权限）才降级；连接/网络错误不能误关渲染，
                    # 否则一次网络抖动就让整个实例永久退回纯文本。
                    if markdown and index == 0 and isinstance(exc, RuntimeError):
                        logger.warning(
                            "qq: markdown 被 API 拒绝，本实例回退纯文本（后续不再尝试）: %s", exc
                        )
                        self._markdown_enabled = False
                        markdown = False
                        fell_back = True
                        break
                    logger.warning("qq: send_text failed for %s: %s", _safe_id(chat_id), exc)
                    return
                if index < len(chunks) - 1:
                    await asyncio.sleep(self.SEND_CHUNK_DELAY_SECONDS)
            if not fell_back:
                return

    async def _send_attachment(self, chat_id: str, attach_path: str, msg_id: Optional[str]) -> None:
        path = Path(attach_path)
        if not path.exists() or not path.is_file():
            logger.warning("qq: attachment not found: %s", attach_path)
            return
        try:
            token = await self._ensure_token()
            await send_media_file(
                self._session,
                token=token,
                chat_type=self._chat_type.get(chat_id, "c2c"),
                target_id=chat_id,
                media_source=str(path),
                msg_id=msg_id,
                msg_seq=self._next_seq(chat_id),
            )
        except Exception as exc:
            logger.warning("qq: send_file failed for %s: %s", attach_path, exc)
