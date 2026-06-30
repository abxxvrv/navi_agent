"""
QQ Bot (official open-platform) transport helpers for Navi.

Low-level layer behind :mod:`navi_agent.gateway.qq`, analogous to what
:mod:`navi_agent.gateway.ilink` is to the WeChat gateway. Holds:

- credential / allowlist persistence under ``<navi_home>/qq/accounts/``;
- access-token + WebSocket-gateway REST calls;
- C2C (private) message / media / typing send helpers;
- the scan-to-configure QR login flow (``qr_login``).

Inbound/outbound orchestration lives in the :class:`~navi_agent.gateway.qq.QqAdapter`.

Reference: https://bot.q.qq.com/wiki/develop/api-v2/
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote, urlparse

from .ilink import _atomic_json_write, _safe_id  # shared, channel-agnostic helpers

logger = logging.getLogger(__name__)

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

# ── Endpoints ─────────────────────────────────────────────────────────────────

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
GATEWAY_URL_PATH = "/gateway"

# Scan-to-configure (QR onboard) portal — q.qq.com hosts the bind task APIs and
# the connect page the user opens from the QQ app.
PORTAL_HOST = os.getenv("QQ_PORTAL_HOST", "q.qq.com")
ONBOARD_CREATE_PATH = "/lite/create_bind_task"
ONBOARD_POLL_PATH = "/lite/poll_bind_result"
QR_URL_TEMPLATE = (
    "https://q.qq.com/qqbot/openclaw/connect.html?task_id={task_id}&_wv=2&source=navi"
)

# ── Timeouts & retry ──────────────────────────────────────────────────────────

API_TIMEOUT_SECONDS = 30.0
UPLOAD_TIMEOUT_SECONDS = 120.0
CONNECT_TIMEOUT_SECONDS = 20.0

RECONNECT_BACKOFF = [2, 5, 10, 30, 60]
MAX_RECONNECT_ATTEMPTS = 100
RATE_LIMIT_DELAY = 60

ONBOARD_POLL_INTERVAL = 2.0
ONBOARD_API_TIMEOUT = 10.0

MESSAGE_DEDUP_TTL_SECONDS = 300
MAX_MESSAGE_LENGTH = 4000

# ── Gateway opcodes / intents ─────────────────────────────────────────────────

OP_DISPATCH = 0
OP_HEARTBEAT = 1
OP_IDENTIFY = 2
OP_RESUME = 6
OP_RECONNECT = 7
OP_INVALID_SESSION = 9
OP_HELLO = 10
OP_HEARTBEAT_ACK = 11

# Only the C2C / group at-message event group (公域消息). We handle C2C only,
# mirroring the WeChat gateway's DM-only experience.
INTENT_GROUP_AND_C2C = 1 << 25

# ── Message / media types ─────────────────────────────────────────────────────

MSG_TYPE_TEXT = 0
MSG_TYPE_MEDIA = 7
MSG_TYPE_INPUT_NOTIFY = 6

MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_VOICE = 3
MEDIA_TYPE_FILE = 4


def build_user_agent() -> str:
    py = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return f"NaviQQBot (Python/{py}; {platform.system().lower()})"


# ── Credential persistence ────────────────────────────────────────────────────


def _account_dir(navi_home: str) -> Path:
    path = Path(navi_home) / "qq" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _account_file(navi_home: str, account_id: str) -> Path:
    return _account_dir(navi_home) / f"{account_id}.json"


def save_qq_account(
    navi_home: str,
    *,
    account_id: str,
    app_id: str,
    client_secret: str,
) -> None:
    """Persist QQ bot credentials (app_id + client_secret) for later reuse."""
    payload = {
        "app_id": app_id,
        "client_secret": client_secret,
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    path = _account_file(navi_home, account_id)
    _atomic_json_write(path, payload)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_qq_account(navi_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    path = _account_file(navi_home, account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_qq_accounts(navi_home: str) -> List[str]:
    directory = _account_dir(navi_home)
    accounts: List[str] = []
    for path in sorted(directory.glob("*.json")):
        name = path.name[: -len(".json")]
        if name.endswith(".allow"):
            continue
        accounts.append(name)
    return accounts


# ── Allowlist ─────────────────────────────────────────────────────────────────


def _allow_file(navi_home: str, account_id: str) -> Path:
    return _account_dir(navi_home) / f"{account_id}.allow.json"


def load_qq_allowlist(navi_home: str, account_id: str) -> List[str]:
    """Return the user openids permitted to drive this account's bot."""
    path = _allow_file(navi_home, account_id)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    allowed = data.get("allowed") if isinstance(data, dict) else None
    if not isinstance(allowed, list):
        return []
    return [str(u).strip() for u in allowed if str(u).strip()]


def add_to_qq_allowlist(navi_home: str, account_id: str, user_id: str) -> bool:
    """Add *user_id* to the allowlist. Returns True if newly added."""
    user_id = str(user_id).strip()
    if not user_id:
        raise ValueError("user_id 不能为空")
    current = load_qq_allowlist(navi_home, account_id)
    if user_id in current:
        return False
    current.append(user_id)
    _atomic_json_write(_allow_file(navi_home, account_id), {"allowed": current})
    return True


def remove_from_qq_allowlist(navi_home: str, account_id: str, user_id: str) -> bool:
    """Remove *user_id* from the allowlist. Returns True if it was present."""
    user_id = str(user_id).strip()
    current = load_qq_allowlist(navi_home, account_id)
    if user_id not in current:
        return False
    _atomic_json_write(
        _allow_file(navi_home, account_id),
        {"allowed": [u for u in current if u != user_id]},
    )
    return True


# ── REST helpers ──────────────────────────────────────────────────────────────


async def get_access_token(
    session: "aiohttp.ClientSession", *, app_id: str, client_secret: str
) -> Dict[str, Any]:
    """Fetch an app access token. Returns ``{access_token, expires_in}``."""
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
    async with session.post(
        TOKEN_URL,
        json={"appId": app_id, "clientSecret": client_secret},
        headers={"Content-Type": "application/json", "User-Agent": build_user_agent()},
        timeout=timeout,
    ) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"QQ getAppAccessToken HTTP {response.status}: {raw[:200]}")
        data = json.loads(raw)
    if not data.get("access_token"):
        raise RuntimeError(f"QQ token response missing access_token: {data}")
    return data


async def get_gateway_url(session: "aiohttp.ClientSession", *, token: str) -> str:
    """Fetch the WebSocket gateway URL."""
    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT_SECONDS)
    async with session.get(
        f"{API_BASE}{GATEWAY_URL_PATH}",
        headers={"Authorization": f"QQBot {token}", "User-Agent": build_user_agent()},
        timeout=timeout,
    ) as response:
        raw = await response.text()
        if not response.ok:
            raise RuntimeError(f"QQ gateway HTTP {response.status}: {raw[:200]}")
        data = json.loads(raw)
    url = data.get("url")
    if not url:
        raise RuntimeError(f"QQ gateway response missing url: {data}")
    return str(url)


async def _api_request(
    session: "aiohttp.ClientSession",
    *,
    method: str,
    token: str,
    path: str,
    body: Optional[Dict[str, Any]] = None,
    timeout_seconds: float = API_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Authenticated REST call against ``api.sgroup.qq.com``."""
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
        "User-Agent": build_user_agent(),
    }
    async with session.request(
        method, f"{API_BASE}{path}", headers=headers, json=body, timeout=timeout
    ) as response:
        raw = await response.text()
        data = json.loads(raw) if raw else {}
        if response.status >= 400:
            message = data.get("message", raw[:200]) if isinstance(data, dict) else raw[:200]
            raise RuntimeError(f"QQ API {method} {path} HTTP {response.status}: {message}")
        return data if isinstance(data, dict) else {}


async def send_c2c_text(
    session: "aiohttp.ClientSession",
    *,
    token: str,
    openid: str,
    text: str,
    msg_id: Optional[str],
    msg_seq: int,
) -> Dict[str, Any]:
    """Send a plain-text C2C message. Pass *msg_id* for a (free) passive reply."""
    body: Dict[str, Any] = {
        "content": text[:MAX_MESSAGE_LENGTH],
        "msg_type": MSG_TYPE_TEXT,
        "msg_seq": msg_seq,
    }
    if msg_id:
        body["msg_id"] = msg_id
    return await _api_request(
        session, method="POST", token=token, path=f"/v2/users/{openid}/messages", body=body
    )


async def send_c2c_typing(
    session: "aiohttp.ClientSession",
    *,
    token: str,
    openid: str,
    msg_id: str,
    msg_seq: int,
    input_seconds: int = 30,
) -> Dict[str, Any]:
    """Show the 'bot is typing' indicator for *input_seconds* (passive)."""
    body = {
        "msg_type": MSG_TYPE_INPUT_NOTIFY,
        "msg_id": msg_id,
        "msg_seq": msg_seq,
        "input_notify": {"input_type": 1, "input_second": input_seconds},
    }
    return await _api_request(
        session, method="POST", token=token, path=f"/v2/users/{openid}/messages", body=body
    )


async def send_c2c_file(
    session: "aiohttp.ClientSession",
    *,
    token: str,
    openid: str,
    path: Path,
    msg_id: Optional[str],
    msg_seq: int,
) -> Dict[str, Any]:
    """Upload a local file and send it as a C2C rich-media message.

    The media type is inferred from the file suffix; everything that is not an
    image / video / audio is sent as a generic file.
    """
    file_type = _media_type_for(path)
    upload_body: Dict[str, Any] = {
        "file_type": file_type,
        "file_data": base64.b64encode(path.read_bytes()).decode("ascii"),
        "srv_send_msg": False,
    }
    if file_type == MEDIA_TYPE_FILE:
        upload_body["file_name"] = path.name
    upload = await _api_request(
        session,
        method="POST",
        token=token,
        path=f"/v2/users/{openid}/files",
        body=upload_body,
        timeout_seconds=UPLOAD_TIMEOUT_SECONDS,
    )
    file_info = upload.get("file_info")
    if not file_info:
        raise RuntimeError(f"QQ file upload returned no file_info: {upload}")

    body: Dict[str, Any] = {
        "msg_type": MSG_TYPE_MEDIA,
        "media": {"file_info": file_info},
        "msg_seq": msg_seq,
    }
    if msg_id:
        body["msg_id"] = msg_id
    return await _api_request(
        session, method="POST", token=token, path=f"/v2/users/{openid}/messages", body=body
    )


def _media_type_for(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}:
        return MEDIA_TYPE_IMAGE
    if suffix in {".mp4", ".mov", ".avi", ".mkv"}:
        return MEDIA_TYPE_VIDEO
    if suffix in {".silk", ".amr", ".mp3", ".wav", ".m4a"}:
        return MEDIA_TYPE_VOICE
    return MEDIA_TYPE_FILE


# ── Inbound media download ────────────────────────────────────────────────────

# QQ delivers inbound attachments as plain HTTPS URLs on these CDN hosts.
_QQ_MEDIA_HOST_SUFFIXES = (
    ".qq.com",
    ".qq.com.cn",
    ".qpic.cn",
    ".qlogo.cn",
    ".gtimg.cn",
    ".myqcloud.com",
)


def _assert_qq_media_url(url: str) -> None:
    """Raise ValueError if *url* is not an HTTPS QQ CDN URL (SSRF guard)."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"media URL has disallowed scheme {parsed.scheme!r}")
    if not any(host == s.lstrip(".") or host.endswith(s) for s in _QQ_MEDIA_HOST_SUFFIXES):
        raise ValueError(f"media URL host {host!r} is not a QQ CDN host (SSRF guard)")


async def download_inbound_media(
    session: "aiohttp.ClientSession", *, url: str, timeout_seconds: float = 60.0
) -> bytes:
    """Download an inbound attachment URL after validating its host."""
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = "https://" + url
    _assert_qq_media_url(url)

    async def _do() -> bytes:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()

    return await asyncio.wait_for(_do(), timeout=timeout_seconds)


# ── Scan-to-configure QR login ────────────────────────────────────────────────


def _onboard_headers() -> Dict[str, str]:
    # q.qq.com returns an anti-bot challenge page without Accept: application/json.
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": build_user_agent(),
    }


def _decrypt_secret(encrypted_base64: str, key_base64: str) -> str:
    """Decrypt an AES-256-GCM ciphertext: base64(IV[12] ‖ ciphertext ‖ tag[16])."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(key_base64)
    raw = base64.b64decode(encrypted_base64)
    iv, ciphertext_with_tag = raw[:12], raw[12:]
    return AESGCM(key).decrypt(iv, ciphertext_with_tag, None).decode("utf-8")


def _render_qr(url: str) -> None:
    try:
        import qrcode

        qr = qrcode.QRCode(border=2)
        qr.add_data(url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:
        print(f"（终端二维码渲染失败: {exc}，请直接打开上面的链接）")


async def qr_login(navi_home: str, *, timeout_seconds: int = 600) -> Optional[Dict[str, str]]:
    """Run the QQ scan-to-configure flow and persist the resulting credentials.

    Creates a bind task, shows a QR code the user opens in the QQ app, polls for
    completion, decrypts the returned ``client_secret`` locally, and saves the
    account under ``<navi_home>/qq/accounts/``. The scanning user is added to the
    allowlist automatically. Returns a credential dict on success, else ``None``.
    """
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for QQ login (pip install aiohttp)")

    create_url = f"https://{PORTAL_HOST}{ONBOARD_CREATE_PATH}"
    poll_url = f"https://{PORTAL_HOST}{ONBOARD_POLL_PATH}"
    key = base64.b64encode(os.urandom(32)).decode()
    deadline = time.monotonic() + timeout_seconds

    async with aiohttp.ClientSession(trust_env=True) as session:
        for refresh_count in range(4):
            try:
                async with session.post(
                    create_url,
                    json={"key": key},
                    headers=_onboard_headers(),
                    timeout=aiohttp.ClientTimeout(total=ONBOARD_API_TIMEOUT),
                ) as resp:
                    data = json.loads(await resp.text())
            except Exception as exc:
                logger.error("qq: create_bind_task failed: %s", exc)
                return None
            if data.get("retcode") != 0:
                logger.error("qq: create_bind_task error: %s", data.get("msg"))
                return None
            task_id = str((data.get("data") or {}).get("task_id") or "")
            if not task_id:
                logger.error("qq: create_bind_task missing task_id")
                return None

            connect_url = QR_URL_TEMPLATE.format(task_id=quote(task_id))
            print("\n请使用手机 QQ 扫描以下二维码（或直接打开链接）：")
            print(f"  {connect_url}\n")
            _render_qr(connect_url)

            while time.monotonic() < deadline:
                try:
                    async with session.post(
                        poll_url,
                        json={"task_id": task_id},
                        headers=_onboard_headers(),
                        timeout=aiohttp.ClientTimeout(total=ONBOARD_API_TIMEOUT),
                    ) as resp:
                        poll = json.loads(await resp.text())
                except Exception:
                    await asyncio.sleep(ONBOARD_POLL_INTERVAL)
                    continue
                if poll.get("retcode") != 0:
                    await asyncio.sleep(ONBOARD_POLL_INTERVAL)
                    continue

                d = poll.get("data") or {}
                status = int(d.get("status", 0))
                if status == 2:  # COMPLETED
                    app_id = str(d.get("bot_appid") or "")
                    user_openid = str(d.get("user_openid") or "")
                    try:
                        client_secret = _decrypt_secret(str(d.get("bot_encrypt_secret") or ""), key)
                    except Exception as exc:
                        logger.error("qq: failed to decrypt client_secret: %s", exc)
                        return None
                    if not app_id or not client_secret:
                        logger.error("qq: bind completed but credential payload was incomplete")
                        return None
                    save_qq_account(
                        navi_home, account_id=app_id, app_id=app_id, client_secret=client_secret
                    )
                    if user_openid:
                        add_to_qq_allowlist(navi_home, app_id, user_openid)
                    print(f"\nQQ 连接成功，account_id={app_id}")
                    return {"account_id": app_id, "user_openid": user_openid}
                if status == 3:  # EXPIRED
                    print(f"\n二维码已过期，正在刷新... ({refresh_count + 1}/3)")
                    break
                await asyncio.sleep(ONBOARD_POLL_INTERVAL)
            else:
                print("\nQQ 登录超时。")
                return None

    print("\n二维码多次过期，请重新执行登录。")
    return None
