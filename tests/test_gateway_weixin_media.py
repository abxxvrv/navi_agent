"""Tests for inbound WeChat media support in the ilink and weixin gateway modules."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from navi_agent.gateway.ilink import (
    ITEM_FILE,
    ITEM_IMAGE,
    ITEM_VIDEO,
    ITEM_VOICE,
    WEIXIN_CDN_BASE_URL,
    _aes128_ecb_decrypt,
    _aes128_ecb_encrypt,
    _assert_weixin_cdn_url,
    _cdn_download_url,
    _parse_aes_key,
    download_inbound_media,
)


# ── ilink unit tests ──────────────────────────────────────────────────────────


def test_decrypt_is_inverse_of_encrypt():
    key = bytes.fromhex("0102030405060708090a0b0c0d0e0f10")
    plaintext = b"hello weixin media!"
    ciphertext = _aes128_ecb_encrypt(plaintext, key)
    assert ciphertext != plaintext
    assert _aes128_ecb_decrypt(ciphertext, key) == plaintext


def test_decrypt_roundtrip_exact_block():
    """Plaintext that is exactly one block long (16 bytes)."""
    key = bytes(range(16))
    plaintext = b"0123456789abcdef"
    assert _aes128_ecb_decrypt(_aes128_ecb_encrypt(plaintext, key), key) == plaintext


def test_parse_aes_key_16_raw_bytes():
    raw = bytes(range(16))
    b64 = base64.b64encode(raw).decode("ascii")
    assert _parse_aes_key(b64) == raw


def test_parse_aes_key_32_hex_bytes():
    """Key stored as base64(hex_string) — the format iLink uses for outbound."""
    raw_hex = "0102030405060708090a0b0c0d0e0f10"
    b64 = base64.b64encode(raw_hex.encode("ascii")).decode("ascii")
    assert _parse_aes_key(b64) == bytes.fromhex(raw_hex)


def test_parse_aes_key_invalid_raises():
    with pytest.raises(ValueError):
        _parse_aes_key(base64.b64encode(b"\x00" * 5).decode("ascii"))


def test_cdn_download_url_encodes_param():
    url = _cdn_download_url("https://novac2c.cdn.weixin.qq.com/c2c", "abc/def==")
    assert url.startswith("https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=")
    assert "abc%2Fdef%3D%3D" in url or "abc/def" in url  # percent-encoded or safe


def test_assert_weixin_cdn_url_allowlisted():
    # Should not raise
    _assert_weixin_cdn_url("https://novac2c.cdn.weixin.qq.com/c2c/download?foo=bar")


def test_assert_weixin_cdn_url_rejected():
    with pytest.raises(ValueError, match="allowlist"):
        _assert_weixin_cdn_url("https://evil.example.com/payload")


def test_assert_weixin_cdn_url_bad_scheme():
    with pytest.raises(ValueError, match="scheme"):
        _assert_weixin_cdn_url("ftp://novac2c.cdn.weixin.qq.com/file")


# ── download_inbound_media mocked tests ───────────────────────────────────────


def _make_session(response_bytes: bytes) -> MagicMock:
    """Create a mock aiohttp.ClientSession that returns response_bytes on GET."""
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.read = AsyncMock(return_value=response_bytes)
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=False)

    session = MagicMock()
    session.get = MagicMock(return_value=response)
    return session


@pytest.mark.asyncio
async def test_download_via_encrypt_query_param_and_decrypt():
    key = bytes(range(16))
    aes_key_b64 = base64.b64encode(key).decode("ascii")  # 16 raw bytes → 24-char b64
    plaintext = b"image data"
    ciphertext = _aes128_ecb_encrypt(plaintext, key)

    session = _make_session(ciphertext)

    result = await download_inbound_media(
        session,
        cdn_base_url=WEIXIN_CDN_BASE_URL,
        encrypt_query_param="token123",
        aes_key_b64=aes_key_b64,
        full_url=None,
        timeout_seconds=10.0,
    )
    assert result == plaintext
    # Should have fetched the CDN download URL (not full_url)
    call_url = session.get.call_args[0][0]
    assert "download" in call_url
    assert "token123" in call_url


@pytest.mark.asyncio
async def test_download_via_full_url_no_decrypt():
    """full_url branch with no aes_key: raw bytes returned as-is."""
    raw = b"raw file bytes"
    session = _make_session(raw)

    result = await download_inbound_media(
        session,
        cdn_base_url=WEIXIN_CDN_BASE_URL,
        encrypt_query_param=None,
        aes_key_b64=None,
        full_url="https://novac2c.cdn.weixin.qq.com/c2c/download?x=1",
        timeout_seconds=10.0,
    )
    assert result == raw


@pytest.mark.asyncio
async def test_download_full_url_ssrf_blocked():
    session = _make_session(b"")
    with pytest.raises(ValueError, match="allowlist"):
        await download_inbound_media(
            session,
            cdn_base_url=WEIXIN_CDN_BASE_URL,
            encrypt_query_param=None,
            aes_key_b64=None,
            full_url="https://attacker.example.com/evil",
            timeout_seconds=10.0,
        )


@pytest.mark.asyncio
async def test_download_no_param_raises():
    session = _make_session(b"")
    with pytest.raises(RuntimeError, match="neither"):
        await download_inbound_media(
            session,
            cdn_base_url=WEIXIN_CDN_BASE_URL,
            encrypt_query_param=None,
            aes_key_b64=None,
            full_url=None,
            timeout_seconds=10.0,
        )


# ── WeixinAdapter._collect_media tests ───────────────────────────────────────


def _make_adapter(tmp_path: Path) -> Any:
    """Create a WeixinAdapter without hitting the iLink network."""
    # Bypass __init__ which requires stored credentials
    from navi_agent.gateway.weixin import WeixinAdapter

    adapter = WeixinAdapter.__new__(WeixinAdapter)
    adapter._navi_home = str(tmp_path)
    adapter._account_id = "test_account"
    adapter._send_session = MagicMock()  # will be replaced per-test
    return adapter


def _make_download_mock(data: bytes) -> AsyncMock:
    """Return an AsyncMock that patches download_inbound_media to return data."""
    return AsyncMock(return_value=data)


@pytest.mark.asyncio
async def test_collect_media_image(tmp_path):
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_IMAGE,
            "image_item": {
                "media": {"encrypt_query_param": "qp1", "aes_key": None},
            },
        }
    ]
    fake_image = b"\xff\xd8\xff" + b"\x00" * 10  # minimal JPEG-like bytes

    with patch(
        "navi_agent.gateway.weixin.download_inbound_media",
        AsyncMock(return_value=fake_image),
    ):
        image_paths, notes = await adapter._collect_media(item_list)

    assert len(image_paths) == 1
    assert notes == []
    saved = image_paths[0]
    assert saved.exists()
    assert saved.suffix == ".jpg"
    assert saved.read_bytes() == fake_image


@pytest.mark.asyncio
async def test_collect_media_file(tmp_path):
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_FILE,
            "file_item": {
                "file_name": "report.pdf",
                "media": {"encrypt_query_param": "qp2", "aes_key": None},
            },
        }
    ]
    fake_data = b"%PDF content"

    with patch(
        "navi_agent.gateway.weixin.download_inbound_media",
        AsyncMock(return_value=fake_data),
    ):
        image_paths, notes = await adapter._collect_media(item_list)

    assert image_paths == []
    assert len(notes) == 1
    assert notes[0].startswith("[用户发来文件：")
    # Absolute path should be in the note
    assert "report.pdf" in notes[0]
    # The saved file should exist
    note_path_str = notes[0].removeprefix("[用户发来文件：").rstrip("]")
    assert Path(note_path_str).exists()
    assert Path(note_path_str).read_bytes() == fake_data


@pytest.mark.asyncio
async def test_collect_media_video(tmp_path):
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_VIDEO,
            "video_item": {
                "media": {"encrypt_query_param": "qp3", "aes_key": None},
            },
        }
    ]
    fake_data = b"video bytes"

    with patch(
        "navi_agent.gateway.weixin.download_inbound_media",
        AsyncMock(return_value=fake_data),
    ):
        image_paths, notes = await adapter._collect_media(item_list)

    assert image_paths == []
    assert len(notes) == 1
    assert "[用户发来视频：" in notes[0]
    note_path_str = notes[0].removeprefix("[用户发来视频：").rstrip("]")
    assert Path(note_path_str).suffix == ".mp4"


@pytest.mark.asyncio
async def test_collect_media_voice_with_text_skipped(tmp_path):
    """Voice item that has ASR text should not be downloaded."""
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_VOICE,
            "voice_item": {
                "text": "转写文字",
                "media": {"encrypt_query_param": "qp4"},
            },
        }
    ]

    with patch(
        "navi_agent.gateway.weixin.download_inbound_media",
        AsyncMock(return_value=b"audio"),
    ) as mock_dl:
        image_paths, notes = await adapter._collect_media(item_list)

    mock_dl.assert_not_called()
    assert image_paths == []
    assert notes == []


@pytest.mark.asyncio
async def test_collect_media_voice_no_text_downloaded(tmp_path):
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_VOICE,
            "voice_item": {
                "media": {"encrypt_query_param": "qp5", "aes_key": None},
            },
        }
    ]
    fake_data = b"silk audio"

    with patch(
        "navi_agent.gateway.weixin.download_inbound_media",
        AsyncMock(return_value=fake_data),
    ):
        image_paths, notes = await adapter._collect_media(item_list)

    assert image_paths == []
    assert len(notes) == 1
    assert "[用户发来语音文件：" in notes[0]
    note_path_str = notes[0].removeprefix("[用户发来语音文件：").rstrip("]")
    assert Path(note_path_str).suffix == ".silk"


@pytest.mark.asyncio
async def test_collect_media_download_failure_continues(tmp_path):
    """A single-item failure should be logged and skipped, not abort the whole turn."""
    adapter = _make_adapter(tmp_path)
    item_list = [
        {
            "type": ITEM_IMAGE,
            "image_item": {"media": {"encrypt_query_param": "bad"}},
        },
        {
            "type": ITEM_FILE,
            "file_item": {
                "file_name": "ok.txt",
                "media": {"encrypt_query_param": "good", "aes_key": None},
            },
        },
    ]

    call_count = 0

    async def _side_effect(session, *, cdn_base_url, encrypt_query_param, aes_key_b64, full_url, timeout_seconds):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("CDN error")
        return b"file content"

    with patch("navi_agent.gateway.weixin.download_inbound_media", side_effect=_side_effect):
        image_paths, notes = await adapter._collect_media(item_list)

    # Image failed → skipped; file succeeded
    assert image_paths == []
    assert len(notes) == 1
    assert "ok.txt" in notes[0]


# ── _handle_message integration: run_turn receives image_paths and notes ──────


class FakeRuntime:
    """Minimal stand-in for AgentRuntime that records run_turn arguments."""

    def __init__(self):
        self.calls: list = []

    def run_turn(self, text: str, image_paths=None):
        self.calls.append({"text": text, "image_paths": image_paths or []})
        return {"ok": True, "final_answer": "ok", "pending_attachments": []}

    def interrupt(self, reason: str):
        pass


def _make_full_adapter(tmp_path: Path) -> Any:
    """Adapter with all fields needed for _handle_message."""
    from collections import defaultdict
    from navi_agent.gateway.weixin import WeixinAdapter

    adapter = WeixinAdapter.__new__(WeixinAdapter)
    adapter._navi_home = str(tmp_path)
    adapter._account_id = "acct"
    adapter._token = "tok"
    adapter._base_url = "https://ilinkai.weixin.qq.com"
    adapter._workspace = str(tmp_path)
    adapter._approval_mode = "open"
    adapter._send_session = MagicMock()
    adapter._poll_session = MagicMock()

    from navi_agent.gateway.ilink import (
        ContextTokenStore,
        MessageDeduplicator,
        TypingTicketCache,
        MESSAGE_DEDUP_TTL_SECONDS,
    )
    adapter._token_store = ContextTokenStore(str(tmp_path))
    adapter._typing_cache = TypingTicketCache()
    adapter._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)
    adapter._runtimes = {}
    adapter._chat_locks = defaultdict(asyncio.Lock)
    adapter._running = True

    return adapter


@pytest.mark.asyncio
async def test_handle_message_image_passes_image_paths(tmp_path):
    """_handle_message with an ITEM_IMAGE calls run_turn with non-empty image_paths."""
    adapter = _make_full_adapter(tmp_path)

    # Allowlist sender
    from navi_agent.gateway.ilink import _atomic_json_write, _account_dir
    allow_path = _account_dir(str(tmp_path)) / "acct.allow.json"
    _atomic_json_write(allow_path, {"allowed": ["user123"]})

    fake_runtime = FakeRuntime()
    adapter._runtimes["user123"] = fake_runtime

    fake_image_bytes = b"\xff\xd8\xff" + b"\x00" * 8
    message = {
        "from_user_id": "user123",
        "message_id": "msg001",
        "item_list": [
            {
                "type": ITEM_IMAGE,
                "image_item": {
                    "media": {"encrypt_query_param": "qp_img", "aes_key": None},
                },
            }
        ],
    }

    async def _fake_collect(item_list):
        path = Path(adapter._navi_home) / "inbound" / "acct" / "img.jpg"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(fake_image_bytes)
        return [path], []

    with (
        patch.object(adapter, "_collect_media", side_effect=_fake_collect),
        patch.object(adapter, "_fetch_typing_ticket", AsyncMock()),
        patch.object(adapter, "send_text", AsyncMock()),
        patch.object(adapter, "_keep_typing", AsyncMock()),
    ):
        await adapter._handle_message(message)

    assert len(fake_runtime.calls) == 1
    assert len(fake_runtime.calls[0]["image_paths"]) == 1
    assert fake_runtime.calls[0]["image_paths"][0].name == "img.jpg"


@pytest.mark.asyncio
async def test_handle_message_file_passes_note_in_text(tmp_path):
    """_handle_message with ITEM_FILE passes the absolute path note in message_text."""
    adapter = _make_full_adapter(tmp_path)

    from navi_agent.gateway.ilink import _atomic_json_write, _account_dir
    allow_path = _account_dir(str(tmp_path)) / "acct.allow.json"
    _atomic_json_write(allow_path, {"allowed": ["user456"]})

    fake_runtime = FakeRuntime()
    adapter._runtimes["user456"] = fake_runtime

    saved_file = Path(adapter._navi_home) / "inbound" / "acct" / "doc.pdf"
    saved_file.parent.mkdir(parents=True, exist_ok=True)
    saved_file.write_bytes(b"PDF")

    message = {
        "from_user_id": "user456",
        "message_id": "msg002",
        "item_list": [
            {
                "type": ITEM_FILE,
                "file_item": {
                    "file_name": "doc.pdf",
                    "media": {"encrypt_query_param": "qp_file"},
                },
            }
        ],
    }

    async def _fake_collect(item_list):
        return [], [f"[用户发来文件：{saved_file}]"]

    with (
        patch.object(adapter, "_collect_media", side_effect=_fake_collect),
        patch.object(adapter, "_fetch_typing_ticket", AsyncMock()),
        patch.object(adapter, "send_text", AsyncMock()),
        patch.object(adapter, "_keep_typing", AsyncMock()),
    ):
        await adapter._handle_message(message)

    assert len(fake_runtime.calls) == 1
    call_text = fake_runtime.calls[0]["text"]
    assert str(saved_file) in call_text
    assert "[用户发来文件：" in call_text


@pytest.mark.asyncio
async def test_handle_message_empty_drops(tmp_path):
    """A message with no text, no images, and no notes should be silently dropped."""
    adapter = _make_full_adapter(tmp_path)

    from navi_agent.gateway.ilink import _atomic_json_write, _account_dir
    allow_path = _account_dir(str(tmp_path)) / "acct.allow.json"
    _atomic_json_write(allow_path, {"allowed": ["user789"]})

    fake_runtime = FakeRuntime()
    adapter._runtimes["user789"] = fake_runtime

    message = {
        "from_user_id": "user789",
        "message_id": "msg003",
        "item_list": [],
    }

    async def _fake_collect(item_list):
        return [], []

    with patch.object(adapter, "_collect_media", side_effect=_fake_collect):
        await adapter._handle_message(message)

    # run_turn should never be called
    assert fake_runtime.calls == []
