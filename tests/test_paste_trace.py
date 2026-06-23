"""Tests for navi_agent.cli.paste_trace module."""

import json
import os
from unittest.mock import patch

from navi_agent.cli.paste_trace import summarize_text, trace_paste


def test_summarize_text_basic():
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1"}, clear=False):
        result = summarize_text("hello world")
    assert result["len"] == 11
    assert result["newline_count"] == 0
    assert isinstance(result["sha12"], str)
    assert len(result["sha12"]) == 12
    assert result["preview"] == "hello world"


def test_summarize_text_newlines():
    text = "line1\nline2\nline3"
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1"}, clear=False):
        result = summarize_text(text)
    assert result["len"] == len(text)
    assert result["newline_count"] == 2
    assert "\\n" in result["preview"]


def test_summarize_text_preview_truncation():
    text = "a" * 100
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1"}, clear=False):
        result = summarize_text(text)
    assert len(result["preview"]) <= 80


def test_summarize_text_disabled_noops_for_surrogates():
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "0"}, clear=False):
        assert summarize_text("bad\ud800text") == {}


def test_trace_disabled_no_file_created(tmp_path):
    log_path = tmp_path / "paste_trace.jsonl"
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "0", "NAVI_PASTE_TRACE_PATH": str(log_path)}, clear=False):
        trace_paste("test_event", foo="bar")
    assert not log_path.exists()


def test_trace_default_disabled_does_not_write_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "paste_trace.jsonl"
    monkeypatch.delenv("NAVI_PASTE_TRACE", raising=False)
    monkeypatch.setenv("NAVI_PASTE_TRACE_PATH", str(log_path))

    trace_paste("test_event", foo="bar")

    assert not log_path.exists()


def test_trace_enabled_writes_jsonl(tmp_path, monkeypatch):
    log_path = tmp_path / "paste_trace.jsonl"
    monkeypatch.setenv("NAVI_PASTE_TRACE", "1")
    monkeypatch.setenv("NAVI_PASTE_TRACE_PATH", str(log_path))

    trace_paste("test_event", foo="bar")

    assert log_path.exists()
    lines = log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["event"] == "test_event"
    assert record["foo"] == "bar"
    assert "ts" in record


def test_trace_does_not_store_full_text(tmp_path):
    log_path = tmp_path / "paste_trace.jsonl"
    secret = "x" * 1000
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1", "NAVI_PASTE_TRACE_PATH": str(log_path)}, clear=False):
        trace_paste("test_event", text_summary=summarize_text(secret))
    content = log_path.read_text(encoding="utf-8")
    assert secret not in content
    record = json.loads(content.strip())
    summary = record["text_summary"]
    assert summary["len"] == 1000
    assert len(summary["sha12"]) == 12
    assert summary["preview"] == "x" * 80


def test_trace_exception_does_not_raise(tmp_path):
    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1", "NAVI_PASTE_TRACE_PATH": "/nonexistent/path/file.jsonl"}, clear=False):
        trace_paste("test_event")  # should not raise
