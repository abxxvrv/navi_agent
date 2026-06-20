"""Tests for navi_agent.cli.paste_collapse."""
from __future__ import annotations

import pytest
from pathlib import Path

import navi_agent.paths as _paths_module
from navi_agent.cli.paste_collapse import (
    _COLLAPSE_MIN_CHARS,
    _COLLAPSE_MIN_LINES,
    collapse_large_paste,
    expand_paste_references,
)


@pytest.fixture(autouse=True)
def isolate_navi_home(monkeypatch, tmp_path):
    """Redirect NAVI_HOME to a temp directory for every test."""
    monkeypatch.setenv("NAVI_HOME", str(tmp_path))
    # Also patch paths.get_navi_home so the already-imported module picks it up.
    # The function reads os.environ each call, so setting the env var is enough,
    # but we also need to skip the skills/sessions mkdir side-effects.
    yield tmp_path


# ─── collapse_large_paste ────────────────────────────────────────────────────


def test_small_text_returns_normalized_no_file(tmp_path):
    """Text below both thresholds -> no file written, normalized text returned."""
    small = "short line\nsecond line"
    result, paste_file = collapse_large_paste(small, 1)
    assert paste_file is None
    assert result == small
    assert not (tmp_path / "pastes").exists()


def test_large_line_count_collapses(tmp_path):
    """>= 8 lines -> file created, placeholder returned."""
    text = "\n".join(f"line {i}" for i in range(_COLLAPSE_MIN_LINES))
    result, paste_file = collapse_large_paste(text, 1)
    assert paste_file is not None
    assert paste_file.exists()
    assert paste_file.read_text(encoding="utf-8") == text
    assert result == f"[Pasted text #1: {_COLLAPSE_MIN_LINES} lines -> {paste_file}]"


def test_large_char_count_single_line_collapses(tmp_path):
    """Single long line >= 2000 chars -> collapses."""
    text = "x" * _COLLAPSE_MIN_CHARS
    result, paste_file = collapse_large_paste(text, 2)
    assert paste_file is not None
    assert "[Pasted text #2:" in result
    assert f"{_COLLAPSE_MIN_CHARS}" in result or "1 lines" in result


def test_crlf_normalized_in_file_and_placeholder(tmp_path):
    """CRLF and bare CR are normalized to LF in the stored file."""
    text = "line1\r\nline2\rline3\r\nline4\r\nline5\r\nline6\r\nline7\r\nline8"
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    result, paste_file = collapse_large_paste(text, 1)
    assert paste_file is not None
    assert paste_file.read_text(encoding="utf-8") == normalized
    # Placeholder line count reflects normalized line count
    line_count = len(normalized.splitlines())
    assert f"#{1}: {line_count} lines" in result


def test_paste_number_in_placeholder(tmp_path):
    """paste_number is reflected in the placeholder."""
    text = "\n".join(["x"] * _COLLAPSE_MIN_LINES)
    result, _ = collapse_large_paste(text, 42)
    assert "[Pasted text #42:" in result


def test_pastes_dir_created(tmp_path):
    """pastes/ directory is created under NAVI_HOME."""
    text = "\n".join(["y"] * _COLLAPSE_MIN_LINES)
    collapse_large_paste(text, 1)
    assert (tmp_path / "pastes").is_dir()


# ─── expand_paste_references ─────────────────────────────────────────────────


def test_roundtrip(tmp_path):
    """collapse then expand yields the original normalized text."""
    text = "\n".join(f"line {i}" for i in range(_COLLAPSE_MIN_LINES))
    placeholder, paste_file = collapse_large_paste(text, 1)
    assert paste_file is not None
    expanded = expand_paste_references(placeholder)
    assert expanded == text


def test_expand_path_outside_pastes_dir_keeps_placeholder(tmp_path):
    """A placeholder pointing outside pastes/ is kept verbatim (path traversal guard)."""
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    fake_placeholder = f"[Pasted text #1: 10 lines -> {secret}]"
    result = expand_paste_references(fake_placeholder)
    assert result == fake_placeholder
    assert "SECRET" not in result


def test_expand_traversal_attempt_keeps_placeholder(tmp_path):
    """A placeholder using ../ traversal is rejected."""
    # Create a file in pastes/ then craft a path that escapes via ..
    pastes_dir = tmp_path / "pastes"
    pastes_dir.mkdir()
    target = tmp_path / "private.txt"
    target.write_text("PRIVATE", encoding="utf-8")
    traversal_path = pastes_dir / ".." / "private.txt"
    fake_placeholder = f"[Pasted text #1: 5 lines -> {traversal_path}]"
    result = expand_paste_references(fake_placeholder)
    assert result == fake_placeholder
    assert "PRIVATE" not in result


def test_expand_nonexistent_file_keeps_placeholder(tmp_path):
    """Missing paste file -> placeholder is kept, no exception raised."""
    pastes_dir = tmp_path / "pastes"
    pastes_dir.mkdir()
    ghost = pastes_dir / "paste_0001_20000101_000000.txt"
    fake_placeholder = f"[Pasted text #1: 5 lines -> {ghost}]"
    result = expand_paste_references(fake_placeholder)
    assert result == fake_placeholder


def test_expand_two_placeholders_with_prose(tmp_path):
    """Two placeholders embedded in prose both expand correctly."""
    text1 = "\n".join(f"alpha line {i}" for i in range(_COLLAPSE_MIN_LINES))
    text2 = "\n".join(f"beta line {i}" for i in range(_COLLAPSE_MIN_LINES))
    ph1, _ = collapse_large_paste(text1, 1)
    ph2, _ = collapse_large_paste(text2, 2)
    combined = f"See this: {ph1} and also {ph2} done."
    expanded = expand_paste_references(combined)
    assert "alpha line 0" in expanded
    assert "beta line 0" in expanded
    assert "See this: " in expanded
    assert " and also " in expanded
    assert " done." in expanded


def test_expand_no_placeholders_returns_text_unchanged():
    """Plain text with no placeholders passes through unchanged."""
    text = "Hello, world. No paste here."
    assert expand_paste_references(text) == text
