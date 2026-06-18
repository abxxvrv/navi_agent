"""
iLink (WeChat bot) protocol layer for Navi.

Ported from the Hermes weixin adapter — text-only subset. Provides:
- iLink endpoint constants and request-header construction
- async API helpers (getupdates / sendmessage / sendtyping / getconfig)
- QR login flow
- account-credential, sync-buffer and context-token persistence
- message deduplication and typing-ticket caches
- WeChat-friendly Markdown formatting / chunking

No media (image/voice/file) support: those require AES-128 crypto and are out
of scope for the basic text gateway.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import secrets
import struct
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    default_backend = None  # type: ignore[assignment]
    Cipher = None  # type: ignore[assignment]
    algorithms = None  # type: ignore[assignment]
    modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"

# media_type values for getUploadUrl (distinct from ITEM_* send-message types)
MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2  # iLink frequency limit — backoff and retry
MESSAGE_DEDUP_TTL_SECONDS = 300

WEIXIN_COPY_LINE_WIDTH = 120

ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")


def check_weixin_requirements() -> bool:
    """Return True when runtime dependencies for the text gateway are present."""
    return AIOHTTP_AVAILABLE


def _is_stale_session_ret(
    ret: Optional[int], errcode: Optional[int], errmsg: Optional[str],
) -> bool:
    """True when iLink returns ret=-2 / errcode=-2 with 'unknown error',
    which is a stale-session signal (same as errcode=-14) rather than
    a genuine rate limit."""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "?"
    if len(raw) <= keep:
        return raw
    return raw[:keep]


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    """Return a TCPConnector with a certifi CA bundle, or None if unavailable.

    Tencent's iLink server is not verifiable against some system CA stores.
    When ``certifi`` is installed, use its Mozilla CA bundle; otherwise fall
    back to aiohttp's default (which honors ``SSL_CERT_FILE`` via trust_env).
    """
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx)


# ── Account / sync-buffer / context-token persistence ─────────────────────────


def _atomic_json_write(path: Path, payload: Dict[str, Any]) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _account_dir(navi_home: str) -> Path:
    path = Path(navi_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(navi_home: str, account_id: str) -> Path:
    return _account_dir(navi_home) / f"{account_id}.json"


def save_weixin_account(
    navi_home: str,
    *,
    account_id: str,
    token: str,
    base_url: str,
    user_id: str = "",
) -> None:
    """Persist account credentials for later reuse."""
    payload = {
        "token": token,
        "base_url": base_url,
        "user_id": user_id,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(navi_home, account_id)
    _atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_weixin_account(navi_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted account credentials."""
    path = _account_file(navi_home, account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_weixin_accounts(navi_home: str) -> List[str]:
    """Return account ids that have stored credentials."""
    directory = _account_dir(navi_home)
    accounts: List[str] = []
    for path in sorted(directory.glob("*.json")):
        name = path.name[: -len(".json")]
        if name.endswith(".sync") or name.endswith(".context-tokens"):
            continue
        accounts.append(name)
    return accounts


def _sync_buf_path(navi_home: str, account_id: str) -> Path:
    return _account_dir(navi_home) / f"{account_id}.sync.json"


def _load_sync_buf(navi_home: str, account_id: str) -> str:
    path = _sync_buf_path(navi_home, account_id)
    if not path.exists():
        return ""
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("get_updates_buf", "")
    except Exception:
        return ""


def _save_sync_buf(navi_home: str, account_id: str, sync_buf: str) -> None:
    _atomic_json_write(_sync_buf_path(navi_home, account_id), {"get_updates_buf": sync_buf})


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account + peer."""

    def __init__(self, navi_home: str):
        self._root = _account_dir(navi_home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin: failed to restore context tokens for %s: %s", _safe_id(account_id), exc)
            return
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def drop(self, account_id: str, user_id: str) -> None:
        self._cache.pop(self._key(account_id, user_id), None)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {
            key[len(prefix):]: value
            for key, value in self._cache.items()
            if key.startswith(prefix)
        }
        try:
            _atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning("weixin: failed to persist context tokens for %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing-ticket cache from ``getconfig``."""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if not entry:
            return None
        if time.time() - entry[1] >= self._ttl_seconds:
            self._cache.pop(user_id, None)
            return None
        return entry[0]

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


class MessageDeduplicator:
    """TTL-based message deduplication cache."""

    def __init__(self, max_size: int = 2000, ttl_seconds: float = 300):
        self._seen: Dict[str, float] = {}
        self._max_size = max_size
        self._ttl = ttl_seconds

    def is_duplicate(self, msg_id: str) -> bool:
        if not msg_id:
            return False
        now = time.time()
        if msg_id in self._seen:
            if now - self._seen[msg_id] < self._ttl:
                return True
            del self._seen[msg_id]
        self._seen[msg_id] = now
        if len(self._seen) > self._max_size:
            cutoff = now - self._ttl
            self._seen = {k: v for k, v in self._seen.items() if v > cutoff}
            if len(self._seen) > self._max_size:
                newest = sorted(self._seen.items(), key=lambda item: item[1])[-self._max_size:]
                self._seen = dict(newest)
        return False


# ── Low-level API helpers ─────────────────────────────────────────────────────


async def _api_post(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    payload: Dict[str, Any],
    token: Optional[str],
    timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    url = f"{base_url.rstrip('/')}/{endpoint}"
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.post(url, data=body, headers=_headers(token, body), timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink POST {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


async def _api_get(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    endpoint: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    headers = {
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    async with session.get(url, headers=headers, timeout=timeout) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"iLink GET {endpoint} HTTP {response.status}: {raw[:200]}")
        return json.loads(raw)


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    """AES-128-ECB encrypt with PKCS7 padding."""
    pad_len = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad_len] * pad_len)
    cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
    enc = cipher.encryptor()
    return enc.update(padded) + enc.finalize()


async def send_file(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    cdn_base_url: str,
    token: str,
    to: str,
    path: str,
    context_token: Optional[str],
    client_id: str,
) -> str:
    """Upload a local file via the iLink AES-CDN protocol and send it.

    Returns client_id on success.

    Pitfalls (from Hermes):
    - aes_key sent to API must be base64(hex_string), NOT base64(raw_bytes);
      using raw bytes makes images appear as grey boxes on the receiver.
    - CDN upload must use POST, not PUT; PUT returns 404.
    """
    import hashlib
    import mimetypes

    if not CRYPTO_AVAILABLE:
        raise RuntimeError(
            "cryptography 未安装，无法发送文件。请执行: pip install cryptography"
        )

    plaintext = Path(path).read_bytes()
    filename = Path(path).name
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"

    filekey = secrets.token_hex(16)
    aes_key = secrets.token_bytes(16)
    rawsize = len(plaintext)
    rawfilemd5 = hashlib.md5(plaintext).hexdigest()
    # AES-ECB padded size: ceil((rawsize+1) / 16) * 16
    filesize = ((rawsize + 1 + 15) // 16) * 16

    if mime.startswith("image/"):
        media_type, item_type = MEDIA_IMAGE, ITEM_IMAGE
    elif mime.startswith("video/"):
        media_type, item_type = MEDIA_VIDEO, ITEM_VIDEO
    else:
        media_type, item_type = MEDIA_FILE, ITEM_FILE

    upload_resp = await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey,
            "media_type": media_type,
            "to_user_id": to,
            "rawsize": rawsize,
            "rawfilemd5": rawfilemd5,
            "filesize": filesize,
            "no_need_thumb": True,
            "aeskey": aes_key.hex(),
        },
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )

    upload_full_url = str(upload_resp.get("upload_full_url") or "")
    upload_param = str(upload_resp.get("upload_param") or "")
    if upload_full_url:
        upload_url = upload_full_url
    elif upload_param:
        from urllib.parse import quote
        upload_url = (
            f"{cdn_base_url.rstrip('/')}/upload"
            f"?encrypted_query_param={quote(upload_param, safe='')}"
            f"&filekey={quote(filekey, safe='')}"
        )
    else:
        raise RuntimeError(
            f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_resp}"
        )

    ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

    # POST (not PUT) ciphertext to CDN; response header x-encrypted-param is the media ref
    async with session.post(
        upload_url,
        data=ciphertext,
        headers={"Content-Type": "application/octet-stream"},
        timeout=aiohttp.ClientTimeout(total=120),
    ) as response:
        if response.status != 200:
            raw = await response.text()
            raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")
        encrypted_query_param = response.headers.get("x-encrypted-param", "")
        await response.read()
    if not encrypted_query_param:
        raise RuntimeError("CDN upload response missing x-encrypted-param header")

    # Key pitfall: base64(hex_string), not base64(raw_bytes)
    aes_key_for_api = base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii")

    media_ref = {
        "encrypt_query_param": encrypted_query_param,
        "aes_key": aes_key_for_api,
        "encrypt_type": 1,
    }
    if item_type == ITEM_IMAGE:
        media_item: Dict[str, Any] = {
            "type": ITEM_IMAGE,
            "image_item": {"media": media_ref, "mid_size": len(ciphertext)},
        }
    elif item_type == ITEM_VIDEO:
        media_item = {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": media_ref,
                "video_size": len(ciphertext),
                "play_length": 0,
                "video_md5": rawfilemd5,
            },
        }
    else:
        media_item = {
            "type": ITEM_FILE,
            "file_item": {"media": media_ref, "file_name": filename, "len": str(rawsize)},
        }

    msg: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [media_item],
    }
    if context_token:
        msg["context_token"] = context_token

    await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": msg},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )
    return client_id


async def _get_updates(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    sync_buf: str,
    timeout_ms: int,
) -> Dict[str, Any]:
    try:
        return await _api_post(
            session,
            base_url=base_url,
            endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf},
            token=token,
            timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_message(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to: str,
    text: str,
    context_token: Optional[str],
    client_id: str,
) -> Dict[str, Any]:
    """Send a text message via iLink sendmessage. Returns the raw response
    dict (may carry error codes such as ``errcode: -14`` for session expiry)."""
    if not text or not text.strip():
        raise ValueError("_send_message: text must not be empty")
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_MESSAGE,
        payload={"msg": message},
        token=token,
        timeout_ms=API_TIMEOUT_MS,
    )


async def _send_typing(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    to_user_id: str,
    typing_ticket: str,
    status: int,
) -> None:
    await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_SEND_TYPING,
        payload={
            "ilink_user_id": to_user_id,
            "typing_ticket": typing_ticket,
            "status": status,
        },
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_config(
    session: "aiohttp.ClientSession",
    *,
    base_url: str,
    token: str,
    user_id: str,
    context_token: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(
        session,
        base_url=base_url,
        endpoint=EP_GET_CONFIG,
        payload=payload,
        token=token,
        timeout_ms=CONFIG_TIMEOUT_MS,
    )


# ── Inbound text extraction ───────────────────────────────────────────────────


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            ref_type = ref_item.get("type")
            if ref_type in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts: List[str] = []
                if ref.get("title"):
                    parts.append(str(ref["title"]))
                ref_text = _extract_text([ref_item])
                if ref_text:
                    parts.append(ref_text)
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            voice_text = str((item.get("voice_item") or {}).get("text") or "")
            if voice_text:
                return voice_text
    return ""


# ── WeChat Markdown formatting / chunking ─────────────────────────────────────


def _normalize_markdown_blocks(content: str) -> str:
    lines = content.splitlines()
    result: List[str] = []
    in_code_block = False
    blank_run = 0

    for raw_line in lines:
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
            continue
        if in_code_block:
            result.append(line)
            continue
        if not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
            continue
        blank_run = 0
        result.append(line)

    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_weixin(content: str) -> str:
    """Wrap long display lines that are hard to copy in WeChat clients."""
    if not content:
        return content

    wrapped: List[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
            continue

        if (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
            continue

        wrapped_lines = textwrap.wrap(
            line,
            width=WEIXIN_COPY_LINE_WIDTH,
            break_long_words=False,
            break_on_hyphens=False,
            replace_whitespace=False,
            drop_whitespace=True,
        )
        wrapped.extend(wrapped_lines or [line])

    return "\n".join(wrapped).strip()


def format_message(content: Optional[str]) -> str:
    if content is None:
        return ""
    return _wrap_copy_friendly_lines_for_weixin(_normalize_markdown_blocks(content))


def _split_markdown_blocks(content: str) -> List[str]:
    if not content:
        return []

    blocks: List[str] = []
    current: List[str] = []
    in_code_block = False

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block and current:
                blocks.append("\n".join(current).strip())
                current = []
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        if in_code_block:
            current.append(line)
            continue
        if not line.strip():
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            continue
        current.append(line)

    if current:
        blocks.append("\n".join(current).strip())
    return [block for block in blocks if block]


def _hard_split(text: str, max_length: int) -> List[str]:
    return [text[i:i + max_length] for i in range(0, len(text), max_length)] or [text]


def _split_text_for_weixin_delivery(content: str, max_length: int) -> List[str]:
    """Split content into sequential WeChat messages.

    Keep everything in a single message when it fits the platform limit;
    otherwise pack Markdown blocks (fenced code blocks kept intact) up to
    ``max_length`` and hard-split any single oversized block.
    """
    if not content:
        return []
    if len(content) <= max_length:
        return [content]

    packed: List[str] = []
    current = ""
    for block in _split_markdown_blocks(content):
        candidate = block if not current else f"{current}\n\n{block}"
        if len(candidate) <= max_length:
            current = candidate
            continue
        if current:
            packed.append(current)
            current = ""
        if len(block) <= max_length:
            current = block
        else:
            packed.extend(_hard_split(block, max_length))
    if current:
        packed.append(current)
    return packed or [content]


# ── QR login ──────────────────────────────────────────────────────────────────


async def qr_login(
    navi_home: str,
    *,
    bot_type: str = "3",
    timeout_seconds: int = 480,
) -> Optional[Dict[str, str]]:
    """Run the interactive iLink QR login flow.

    Returns a credential dict on success, or ``None`` if login fails / times out.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")

    async with aiohttp.ClientSession(trust_env=True, connector=_make_ssl_connector()) as session:
        try:
            qr_resp = await _api_get(
                session,
                base_url=ILINK_BASE_URL,
                endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                timeout_ms=QR_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None

        qrcode_value = str(qr_resp.get("qrcode") or "")
        qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None

        qr_scan_data = qrcode_url if qrcode_url else qrcode_value

        print("\n请使用微信扫描以下二维码：")
        if qrcode_url:
            print(qrcode_url)
        try:
            import qrcode

            qr = qrcode.QRCode()
            qr.add_data(qr_scan_data)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except Exception as _qr_exc:
            print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")

        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0

        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session,
                    base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}",
                    timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue

            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qr_resp = await _api_get(
                        session,
                        base_url=ILINK_BASE_URL,
                        endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}",
                        timeout_ms=QR_TIMEOUT_MS,
                    )
                    qrcode_value = str(qr_resp.get("qrcode") or "")
                    qrcode_url = str(qr_resp.get("qrcode_img_content") or "")
                    qr_scan_data = qrcode_url if qrcode_url else qrcode_value
                    if qrcode_url:
                        print(qrcode_url)
                    try:
                        import qrcode as _qrcode

                        qr = _qrcode.QRCode()
                        qr.add_data(qr_scan_data)
                        qr.make(fit=True)
                        qr.print_ascii(invert=True)
                    except Exception:
                        pass
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload was incomplete")
                    return None
                save_weixin_account(
                    navi_home,
                    account_id=account_id,
                    token=token,
                    base_url=base_url,
                    user_id=user_id,
                )
                print(f"\n微信连接成功，account_id={account_id}")
                return {
                    "account_id": account_id,
                    "token": token,
                    "base_url": base_url,
                    "user_id": user_id,
                }
            await asyncio.sleep(1)

        print("\n微信登录超时。")
        return None
