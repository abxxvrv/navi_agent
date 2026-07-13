"""Web 网关测试：token 鉴权、事件透传、审批往返、中断解锁。

不依赖 pytest-asyncio：每个用例用 asyncio.run 包一个协程场景。
runtime 用 stub 替换（monkeypatch WebAdapter.create_runtime），不触真实模型。
"""

from __future__ import annotations

import asyncio
import base64
import threading
from pathlib import Path
from typing import Any, Callable

import pytest

aiohttp = pytest.importorskip("aiohttp")
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402

from navi_agent.gateway.web import (  # noqa: E402
    MAX_IMAGE_ATTACHMENT_BYTES,
    WebAdapter,
    WebConnection,
)
from navi_agent.storage.history_store import HistoryStore  # noqa: E402
from navi_agent.tools.approval import (  # noqa: E402
    ApprovalAction,
    ApprovalDecision,
    RiskLevel,
)

TOKEN = "t0ken"


class FakeRouter:
    context_window = 100_000
    model_name = "fake-model"

    def list_providers(self):
        return ["fake"]

    def list_models(self, provider=None):
        return {"fake-model": {}}


class FakeSessionStore:
    session_id = "sess-test"


class FakeRuntime:
    def __init__(self, event_handler, approval_handler, script):
        self.event_handler = event_handler
        self.approval_handler = approval_handler
        self.script = script
        self.router = FakeRouter()
        self.session_store = FakeSessionStore()
        self.last_usage = {"prompt_tokens": 5000}
        self.conversation_history: list[dict] = []
        self.interrupted: str | None = None

    def run_turn(self, text, image_paths=None):
        return self.script(self, text)

    def interrupt(self, message=None):
        self.interrupted = message or "interrupted"
        # 模拟真实 runtime：TurnScope 的 approval_canceller 会解锁挂起的审批
        cancel = getattr(self.approval_handler, "cancel_current", None)
        if callable(cancel):
            cancel()

    def get_model_info(self):
        return {
            "current_provider": "fake",
            "current_model": "fake-model",
            "current_model_name": "fake-model",
            "providers": ["fake"],
            "models": {},
        }

    def switch_model(self, provider, model):
        return True


def make_adapter(script: Callable[[FakeRuntime, str], dict]) -> WebAdapter:
    adapter = WebAdapter(workspace=".", token=TOKEN)

    def create_runtime(resume_session_id, event_handler, approval_handler):
        return FakeRuntime(event_handler, approval_handler, script)

    adapter.create_runtime = create_runtime  # type: ignore[method-assign]
    return adapter


def test_web_runtime_uses_cli_channel(monkeypatch, tmp_path):
    import navi_agent.gateway.web as web_mod

    captured: dict[str, Any] = {}

    class FakeAgentRuntime:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(web_mod, "AgentRuntime", FakeAgentRuntime)

    adapter = WebAdapter(workspace=tmp_path, token=TOKEN)
    runtime = adapter.create_runtime("sess", lambda _event: None, object())

    assert isinstance(runtime, FakeAgentRuntime)
    assert captured["channel"] == "cli"


def test_hosted_gateways_use_their_default_workspaces(monkeypatch, tmp_path):
    import navi_agent.gateway.ilink as ilink_mod
    import navi_agent.gateway.qq as qq_mod
    import navi_agent.gateway.qqbot as qqbot_mod
    import navi_agent.gateway.web as web_mod
    import navi_agent.gateway.weixin as weixin_mod

    captured: dict[str, tuple[str, Path, str]] = {}

    class FakeQqAdapter:
        def __init__(self, account, *, workspace, approval_mode):
            captured["qq"] = (account, Path(workspace), approval_mode)

    class FakeWeixinAdapter:
        def __init__(self, account, *, workspace, approval_mode):
            captured["weixin"] = (account, Path(workspace), approval_mode)

    monkeypatch.setattr(web_mod, "get_navi_home", lambda: tmp_path)
    monkeypatch.setattr(qqbot_mod, "list_qq_accounts", lambda navi_home: ["qq-1"])
    monkeypatch.setattr(ilink_mod, "list_weixin_accounts", lambda navi_home: ["wx-1"])
    monkeypatch.setattr(qq_mod, "QqAdapter", FakeQqAdapter)
    monkeypatch.setattr(weixin_mod, "WeixinAdapter", FakeWeixinAdapter)

    adapter = WebAdapter(workspace=tmp_path / "web-start", token=TOKEN)

    list_qq, make_qq = adapter._gateway_spec("qq")
    assert list_qq() == ["qq-1"]
    make_qq("qq-1")
    assert captured["qq"] == ("qq-1", tmp_path / "qq" / "workspace", "open")
    assert captured["qq"][1].is_dir()

    list_wx, make_wx = adapter._gateway_spec("weixin")
    assert list_wx() == ["wx-1"]
    make_wx("wx-1")
    assert captured["weixin"] == ("wx-1", tmp_path / "weixin" / "workspace", "open")
    assert captured["weixin"][1].is_dir()


async def recv_until(ws, mtype: str, timeout: float = 5.0) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    while True:
        event = await asyncio.wait_for(ws.receive_json(), timeout)
        events.append(event)
        if event["type"] == mtype:
            return events


ASK_DECISION = ApprovalDecision(
    action=ApprovalAction.ASK,
    risk=RiskLevel.RISKY,
    reason="test reason",
    tool_name="run_command",
    tool_args={"command": "git push"},
    command="git push",
)


def test_token_required(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVI_HOME", str(tmp_path))

    async def scenario():
        adapter = make_adapter(lambda rt, text: {"ok": True, "final_answer": ""})
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            assert (await client.get("/")).status == 403
            assert (await client.get("/api/sessions")).status == 403
            assert (await client.get("/ws")).status == 403
            resp = await client.get(f"/api/sessions?token={TOKEN}")
            assert resp.status == 200
            assert await resp.json() == []
        finally:
            await client.close()

    asyncio.run(scenario())


def test_session_list_refreshes_for_gateway_created_sessions():
    html = (
        Path(__file__).parents[1] / "navi_agent" / "gateway" / "webui" / "index.html"
    ).read_text(encoding="utf-8")

    assert "setInterval(loadSessions, 10000);" in html
    assert 'case "read_only":' in html
    assert "网关会话不可发送" in html


def test_event_stream_and_turn_end():
    def script(rt: FakeRuntime, text: str) -> dict:
        rt.event_handler({"type": "assistant_delta", "delta": "你好"})
        return {"ok": True, "final_answer": "你好！"}

    async def scenario():
        adapter = make_adapter(script)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}")
            init = await asyncio.wait_for(ws.receive_json(), 5)
            assert init["type"] == "init"
            assert init["session_id"] == "sess-test"
            assert init["model_name"] == "fake-model"

            await ws.send_json({"type": "user_message", "text": "hi"})
            events = await recv_until(ws, "turn_end")
            types = [e["type"] for e in events]
            assert "assistant_delta" in types
            turn_end = events[-1]
            assert turn_end["ok"] is True
            assert turn_end["final_answer"] == "你好！"
            assert turn_end["context_pct"] == 5
        finally:
            await client.close()

    asyncio.run(scenario())


def test_history_arrives_before_runtime_initialization(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVI_HOME", str(tmp_path))
    store = HistoryStore(tmp_path / "history.sqlite3", project_path=tmp_path)
    store.append_message({"role": "user", "content": "之前的问题"})
    store.append_message({"role": "assistant", "content": "之前的回答"})

    release_runtime = threading.Event()
    adapter = make_adapter(lambda rt, text: {"ok": True, "final_answer": ""})
    original_create_runtime = adapter.create_runtime

    def create_runtime(resume_session_id, event_handler, approval_handler):
        release_runtime.wait(timeout=5)
        return original_create_runtime(resume_session_id, event_handler, approval_handler)

    adapter.create_runtime = create_runtime  # type: ignore[method-assign]

    async def scenario():
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                f"/ws?token={TOKEN}&session={store.session_id}"
            )
            history = await asyncio.wait_for(ws.receive_json(), 2)
            assert history == {
                "type": "history",
                "session_id": store.session_id,
                "history": [
                    {"role": "user", "content": "之前的问题"},
                    {"role": "assistant", "content": "之前的回答"},
                ],
            }
        finally:
            release_runtime.set()
            await client.close()

    asyncio.run(scenario())


def test_gateway_session_is_read_only_and_tracks_history(monkeypatch, tmp_path):
    monkeypatch.setenv("NAVI_HOME", str(tmp_path))
    store = HistoryStore(
        tmp_path / "history.sqlite3",
        project_path=tmp_path / "qq" / "workspace",
        channel="",
    )
    store.append_message({"role": "user", "content": "第一条"})
    adapter = make_adapter(lambda rt, text: {"ok": True, "final_answer": ""})
    adapter.create_runtime = lambda *args, **kwargs: pytest.fail(
        "gateway session must not create a runtime"
    )

    async def scenario():
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                f"/ws?token={TOKEN}&session={store.session_id}"
            )
            history = await asyncio.wait_for(ws.receive_json(), 2)
            read_only = await asyncio.wait_for(ws.receive_json(), 2)
            assert history["history"] == [{"role": "user", "content": "第一条"}]
            assert read_only == {
                "type": "read_only",
                "session_id": store.session_id,
                "channel": "qq",
                "workspace": str((tmp_path / "qq" / "workspace").resolve()),
            }

            store.append_message({"role": "assistant", "content": "第二条"})
            updated = await asyncio.wait_for(ws.receive_json(), 3)
            assert updated["type"] == "history"
            assert updated["history"][-1] == {
                "role": "assistant",
                "content": "第二条",
            }
        finally:
            await client.close()

    asyncio.run(scenario())


def test_approval_roundtrip():
    def script(rt: FakeRuntime, text: str) -> dict:
        choice = rt.approval_handler(ASK_DECISION)
        return {"ok": True, "final_answer": f"choice={choice.value}"}

    async def scenario():
        adapter = make_adapter(script)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}")
            await asyncio.wait_for(ws.receive_json(), 5)

            await ws.send_json({"type": "user_message", "text": "do it"})
            events = await recv_until(ws, "approval_request")
            request = events[-1]
            assert request["tool_name"] == "run_command"
            assert request["command"] == "git push"
            assert request["risk"] == "risky"

            await ws.send_json({"type": "approval_response", "choice": "allow_once"})
            events = await recv_until(ws, "turn_end")
            assert events[-1]["final_answer"] == "choice=allow_once"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_interrupt_unblocks_pending_approval():
    def script(rt: FakeRuntime, text: str) -> dict:
        choice = rt.approval_handler(ASK_DECISION)
        return {"ok": False, "error": "用户中断", "final_answer": f"choice={choice.value}"}

    async def scenario():
        adapter = make_adapter(script)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}")
            await asyncio.wait_for(ws.receive_json(), 5)

            await ws.send_json({"type": "user_message", "text": "do it"})
            await recv_until(ws, "approval_request")

            await ws.send_json({"type": "interrupt"})
            events = await recv_until(ws, "turn_end")
            turn_end = events[-1]
            assert turn_end["ok"] is False
            assert turn_end["final_answer"] == "choice=reject"
        finally:
            await client.close()

    asyncio.run(scenario())


def test_missing_session_falls_back_to_new():
    calls: list = []
    adapter = WebAdapter(workspace=".", token=TOKEN)

    def create_runtime(resume_session_id, event_handler, approval_handler):
        calls.append(resume_session_id)
        if resume_session_id:
            raise FileNotFoundError(f"Session not found: {resume_session_id}")
        return FakeRuntime(
            event_handler, approval_handler, lambda r, t: {"ok": True, "final_answer": ""}
        )

    adapter.create_runtime = create_runtime  # type: ignore[method-assign]

    async def scenario():
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}&session=gone-123")
            init = await asyncio.wait_for(ws.receive_json(), 5)
            assert init["type"] == "init"
            assert init["session_id"] == "sess-test"
            assert calls == ["gone-123", None]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_runtime_reused_across_reconnect():
    created: list[FakeRuntime] = []
    adapter = WebAdapter(workspace=".", token=TOKEN)

    def create_runtime(resume_session_id, event_handler, approval_handler):
        rt = FakeRuntime(
            event_handler, approval_handler,
            lambda r, t: (r.event_handler({"type": "assistant_delta", "content": "hi"}),
                          {"ok": True, "final_answer": "hi"})[-1],
        )
        created.append(rt)
        return rt

    adapter.create_runtime = create_runtime  # type: ignore[method-assign]

    async def scenario():
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws1 = await client.ws_connect(f"/ws?token={TOKEN}")
            init1 = await asyncio.wait_for(ws1.receive_json(), 5)
            assert init1["session_id"] == "sess-test"
            await ws1.close()

            # 带 session 重连：复用缓存的 runtime，事件出口重绑到新连接
            ws2 = await client.ws_connect(f"/ws?token={TOKEN}&session=sess-test")
            await asyncio.wait_for(ws2.receive_json(), 5)
            assert len(created) == 1

            await ws2.send_json({"type": "user_message", "text": "hi"})
            events = await recv_until(ws2, "turn_end")
            assert any(e["type"] == "assistant_delta" for e in events)
        finally:
            await client.close()

    asyncio.run(scenario())


def test_save_images_roundtrip():
    data = base64.b64encode(b"fake-png-bytes").decode()
    paths, rejected = WebConnection.save_images(
        [{"name": "a.png", "data": data}, {"name": "bad", "data": "!!!not-base64"}]
    )
    assert rejected == []
    assert len(paths) == 1
    assert paths[0].suffix == ".png"
    assert paths[0].read_bytes() == b"fake-png-bytes"
    paths[0].unlink()


def test_oversized_image_rejected_before_runtime():
    called = False

    def script(rt: FakeRuntime, text: str) -> dict:
        nonlocal called
        called = True
        return {"ok": True, "final_answer": "should not run"}

    async def scenario():
        adapter = make_adapter(script)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}")
            await asyncio.wait_for(ws.receive_json(), 5)

            payload = base64.b64encode(b"x" * (MAX_IMAGE_ATTACHMENT_BYTES + 1)).decode()
            await ws.send_json({
                "type": "user_message",
                "text": "",
                "images": [{"name": "big.png", "data": payload}],
            })
            events = await recv_until(ws, "turn_end")
            assert any(
                e["type"] == "notice" and "图片超过 3MB" in e["text"]
                for e in events
            )
            turn_end = events[-1]
            assert turn_end["ok"] is False
            assert "big.png" in turn_end["error"]
            assert called is False
        finally:
            await client.close()

    asyncio.run(scenario())


def test_keyboard_interrupt_becomes_turn_end():
    def script(rt: FakeRuntime, text: str) -> dict:
        raise KeyboardInterrupt("用户中断")

    async def scenario():
        adapter = make_adapter(script)
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            ws = await client.ws_connect(f"/ws?token={TOKEN}")
            await asyncio.wait_for(ws.receive_json(), 5)

            await ws.send_json({"type": "user_message", "text": "hi"})
            events = await recv_until(ws, "turn_end")
            turn_end = events[-1]
            assert turn_end["ok"] is False
            assert turn_end["error"] == "已中断。"

            # 连接仍然可用：再跑一轮
            await ws.send_json({"type": "user_message", "text": "again"})
            await recv_until(ws, "turn_end")
        finally:
            await client.close()

    asyncio.run(scenario())


class FakeGatewayAdapter:
    def __init__(self):
        self.stopped = asyncio.Event()

    async def run(self):
        await self.stopped.wait()


def test_gateway_start_stop(monkeypatch):
    fake = FakeGatewayAdapter()

    async def scenario():
        adapter = make_adapter(lambda rt, text: {"ok": True, "final_answer": ""})

        def gateway_spec(kind):
            if kind != "qq":
                return None
            return (lambda: ["acc-1"], lambda account: fake)

        adapter._gateway_spec = gateway_spec  # type: ignore[method-assign]
        client = TestClient(TestServer(adapter.build_app()))
        await client.start_server()
        try:
            resp = await client.get(f"/api/gateways?token={TOKEN}")
            status = (await resp.json())["qq"]
            assert status == {"accounts": ["acc-1"], "running": False, "account": None, "error": None}

            resp = await client.post(f"/api/gateways/qq/start?token={TOKEN}", json={})
            assert resp.status == 200
            status = await resp.json()
            assert status["running"] is True
            assert status["account"] == "acc-1"

            # 重复启动被拒
            resp = await client.post(f"/api/gateways/qq/start?token={TOKEN}", json={})
            assert resp.status == 409

            resp = await client.post(f"/api/gateways/qq/stop?token={TOKEN}")
            assert resp.status == 200
            assert (await resp.json())["running"] is False

            # 未知网关
            resp = await client.post(f"/api/gateways/nope/start?token={TOKEN}", json={})
            assert resp.status == 404
        finally:
            await client.close()

    asyncio.run(scenario())
