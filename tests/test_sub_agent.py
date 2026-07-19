from __future__ import annotations

import pytest

from navi_agent.runtime.sub_agent import SubAgent
from navi_agent.storage.agent_store import AgentInstanceStore


def test_subagent_persists_complete_transcript_and_resumes(tmp_path):
    store = AgentInstanceStore(tmp_path / "agents")
    agent_id = store.create(agent_type="explore", tool_names=[])
    agent = SubAgent(
        router=object(),
        tools=[],
        tool_handlers={},
        agent_id=agent_id,
        store=store,
    )
    agent._call_llm = lambda _messages: ("first answer", [])

    agent.run("first question")

    assert store.load_context(agent_id) == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]

    resumed = SubAgent(
        router=object(),
        tools=[],
        tool_handlers={},
        agent_id=agent_id,
        store=store,
    )
    resumed.context = store.load_context(agent_id)
    seen = []

    def answer(messages):
        seen.extend(messages)
        return "second answer", []

    resumed._call_llm = answer
    resumed.run("second question")

    assert seen == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]
    assert store.load_context(agent_id)[-2:] == [
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]


def test_subagent_invalid_tool_json_returns_tool_error_without_execution():
    calls = []
    agent = SubAgent(
        router=object(),
        tools=[],
        tool_handlers={"read_file": lambda **args: calls.append(args)},
        max_steps=2,
    )
    responses = iter([
        (
            "",
            [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": "{"},
            }],
        ),
        ("done", []),
    ])
    agent._call_llm = lambda _messages: next(responses)

    result = agent.run("inspect")

    assert calls == []
    assert result.tool_calls_made[0]["result"]["ok"] is False
    assert "Invalid tool arguments" in result.tool_calls_made[0]["result"]["error"]


def test_subagent_forwards_real_tool_call_id_to_executor():
    calls = []
    agent = SubAgent(
        router=object(),
        tools=[],
        tool_handlers={"read_file": lambda **_args: None},
        tool_executor=lambda call_id, name, args: calls.append((call_id, name, args)) or {"ok": True},
        max_steps=2,
    )
    responses = iter([
        (
            "",
            [{
                "id": "call_7",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
            }],
        ),
        ("done", []),
    ])
    agent._call_llm = lambda _messages: next(responses)

    agent.run("inspect")

    assert calls == [("call_7", "read_file", {"path": "a.py"})]


def test_subagent_rejects_empty_final_response_and_step_overrun():
    empty = SubAgent(router=object(), tools=[], tool_handlers={})
    empty._call_llm = lambda _messages: ("", [])
    with pytest.raises(RuntimeError, match="空响应"):
        empty.run("answer")

    looping = SubAgent(
        router=object(),
        tools=[],
        tool_handlers={"read_file": lambda **_args: {"ok": True}},
        max_steps=1,
    )
    looping._call_llm = lambda _messages: (
        "",
        [{
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }],
    )
    with pytest.raises(RuntimeError, match="最大执行步数"):
        looping.run("loop")
