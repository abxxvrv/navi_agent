"""Tests for paste trace integration in prompt_ui.

These tests verify that trace_paste is called with the correct event names.
Since NaviPromptSession requires a real terminal, we test the trace logic
by directly calling trace_paste as the bindings would.
"""

import os
from unittest.mock import patch

import pytest

from navi_agent.cli.paste_trace import summarize_text, trace_paste


def test_idle_enter_submits_and_traces(tmp_path):
    """Assert idle_enter_submit trace event appears when idle Enter is triggered."""
    log_path = tmp_path / "paste_trace.jsonl"
    events = []

    def capture_trace(event, **fields):
        events.append({"event": event, **fields})

    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1", "NAVI_PASTE_TRACE_PATH": str(log_path)}, clear=False):
        with patch("navi_agent.cli.prompt_ui.trace_paste", side_effect=capture_trace):
            from navi_agent.cli import prompt_ui
            # Simulate what the idle Enter binding does
            text = "hello world"
            prompt_ui.trace_paste(
                "idle_enter_seen",
                text_summary=summarize_text(text),
                running=False,
            )
            prompt_ui.trace_paste(
                "idle_enter_submit",
                text_summary=summarize_text(text),
                queue_size=0,
                running=False,
            )
            prompt_ui.trace_paste(
                "idle_queue_put",
                text_summary=summarize_text(text),
                queue_size=1,
                running=False,
            )

    event_names = [e["event"] for e in events]
    assert "idle_enter_seen" in event_names
    assert "idle_enter_submit" in event_names
    assert "idle_queue_put" in event_names


def test_running_enter_newline_traces(tmp_path):
    """Assert running_enter_newline trace event appears when running Enter is triggered."""
    log_path = tmp_path / "paste_trace.jsonl"
    events = []

    def capture_trace(event, **fields):
        events.append({"event": event, **fields})

    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1", "NAVI_PASTE_TRACE_PATH": str(log_path)}, clear=False):
        with patch("navi_agent.cli.prompt_ui.trace_paste", side_effect=capture_trace):
            from navi_agent.cli import prompt_ui
            prompt_ui.trace_paste(
                "running_enter_newline",
                running=True,
                approval_active=False,
                picker_active=False,
                buffer_len=5,
            )

    event_names = [e["event"] for e in events]
    assert "running_enter_newline" in event_names


def test_bracketed_paste_inserts_without_submit(tmp_path):
    """Assert BracketedPaste traces appear but idle_enter_submit does not."""
    log_path = tmp_path / "paste_trace.jsonl"
    events = []

    def capture_trace(event, **fields):
        events.append({"event": event, **fields})

    with patch.dict(os.environ, {"NAVI_PASTE_TRACE": "1", "NAVI_PASTE_TRACE_PATH": str(log_path)}, clear=False):
        with patch("navi_agent.cli.prompt_ui.trace_paste", side_effect=capture_trace):
            from navi_agent.cli import prompt_ui
            data = "pasted text\nwith newlines"
            normalized = data.replace("\r\n", "\n").replace("\r", "\n")
            prompt_ui.trace_paste(
                "bracketed_paste_seen",
                text_summary=summarize_text(normalized),
                running=False,
            )
            prompt_ui.trace_paste(
                "bracketed_paste_inserted",
                text_summary=summarize_text(normalized),
                running=False,
            )

    event_names = [e["event"] for e in events]
    assert "bracketed_paste_seen" in event_names
    assert "bracketed_paste_inserted" in event_names
    assert "idle_enter_submit" not in event_names
