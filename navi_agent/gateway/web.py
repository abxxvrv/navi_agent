"""Web UI gateway: serve a local single-page UI bridged to AgentRuntime.

浏览器 ── WebSocket /ws ── WebAdapter ── AgentRuntime(channel="cli")

事件流：runtime 的 event_handler 在工作线程被调用，经 call_soon_threadsafe
投进 asyncio 队列，由发送协程推给浏览器；审批则反向：runtime 线程阻塞在
Future 上，浏览器点按钮后经 WS 解锁。

Entry point: ``asyncio.run(WebAdapter(...).run())``（CLI: ``navi web``）。
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import contextlib
import json
import logging
import secrets
import tempfile
from pathlib import Path
from typing import Any, Optional

try:
    import aiohttp
    from aiohttp import web
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]
    web = None  # type: ignore[assignment]

from ..paths import get_navi_home
from ..runtime.agent import AgentRuntime
from ..storage.history_store import HistoryStore
from ..tools.approval import ApprovalDecision, UserApprovalChoice

logger = logging.getLogger(__name__)

WEBUI_DIR = Path(__file__).parent / "webui"
MAX_IMAGE_ATTACHMENT_BYTES = 3 * 1000 * 1000


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, default=str)


class WebConnection:
    """一条 WebSocket 连接：一个 AgentRuntime 加事件/审批桥接。"""

    def __init__(
        self,
        adapter: "WebAdapter",
        resume_session_id: Optional[str],
        loop: asyncio.AbstractEventLoop,
    ):
        # 构建 AgentRuntime 是慢的同步操作（读配置、MCP 发现），由 handle_ws
        # 放在线程里执行，所以 loop 需要显式传入而不能 get_running_loop()。
        self._loop = loop
        self.queue: asyncio.Queue[Optional[dict]] = asyncio.Queue()
        self._pending_approval: Optional[concurrent.futures.Future] = None
        self.busy = False

        def approval_handler(decision: ApprovalDecision) -> UserApprovalChoice:
            return self._wait_approval(decision)

        # runtime.interrupt() 经 TurnScope 调用 cancel_current 解锁挂起的审批
        approval_handler.cancel_current = self._cancel_approval  # type: ignore[attr-defined]

        cached = adapter._runtime_cache.get(resume_session_id or "")
        if cached is not None:
            # 复用已有 runtime，把事件/审批出口重绑到本连接
            cached.event_handler = self._emit_threadsafe
            cached.approval_handler = approval_handler
            self.runtime = cached
        else:
            try:
                self.runtime = adapter.create_runtime(
                    resume_session_id=resume_session_id,
                    event_handler=self._emit_threadsafe,
                    approval_handler=approval_handler,
                )
            except FileNotFoundError:
                # 会话在库中不存在（如首轮失败未持久化就进了 URL）。
                # 回退到新会话，否则浏览器会陷入 重连→崩溃 死循环。
                logger.warning("web: session %s not found, starting fresh", resume_session_id)
                self.runtime = adapter.create_runtime(
                    resume_session_id=None,
                    event_handler=self._emit_threadsafe,
                    approval_handler=approval_handler,
                )
            adapter._runtime_cache[self.runtime.session_store.session_id] = self.runtime
        self._adapter = adapter

    # ── runtime 线程 → 事件队列 ──────────────────────────────────────────────

    def _emit_threadsafe(self, event: dict[str, Any]) -> None:
        self._loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def _wait_approval(self, decision: ApprovalDecision) -> UserApprovalChoice:
        future: concurrent.futures.Future = concurrent.futures.Future()
        self._pending_approval = future
        self._emit_threadsafe(
            {
                "type": "approval_request",
                "tool_name": decision.tool_name,
                "command": decision.command,
                "risk": decision.risk.value,
                "reason": decision.reason,
            }
        )
        try:
            return future.result()
        finally:
            self._pending_approval = None

    def _cancel_approval(self) -> None:
        future = self._pending_approval
        if future is not None and not future.done():
            future.set_result(UserApprovalChoice.REJECT)

    # ── 浏览器 → runtime ────────────────────────────────────────────────────

    def resolve_approval(self, choice: str) -> None:
        future = self._pending_approval
        if future is None or future.done():
            return
        try:
            future.set_result(UserApprovalChoice(choice))
        except ValueError:
            future.set_result(UserApprovalChoice.REJECT)

    async def run_turn(self, text: str, image_paths: list[Path]) -> None:
        self.busy = True
        try:
            result = await asyncio.to_thread(self.runtime.run_turn, text, image_paths)
        except KeyboardInterrupt:
            # run_turn 用 KeyboardInterrupt 表达用户中断（见 agent._invoke_agent），
            # 它是 BaseException，任其穿透会把整个事件循环连同进程带崩。
            result = {"ok": False, "error": "已中断。", "final_answer": ""}
        except Exception as exc:
            logger.error("web: run_turn failed: %s", exc)
            result = {"ok": False, "error": str(exc), "final_answer": ""}
        finally:
            self.busy = False
        self._emit_threadsafe(self._turn_end_payload(result))

    def _turn_end_payload(self, result: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "turn_end",
            "ok": bool(result.get("ok")),
            "error": result.get("error"),
            "final_answer": result.get("final_answer") or "",
            "pending_attachments": [str(p) for p in result.get("pending_attachments") or []],
            **self._status(),
        }

    def _status(self) -> dict[str, Any]:
        usage = self.runtime.last_usage
        window = self.runtime.router.context_window
        pct = round((usage.get("prompt_tokens", 0) if usage else 0) / window * 100) if window else 0
        return {
            "model_name": self.runtime.router.model_name,
            "context_pct": pct,
            "session_id": self.runtime.session_store.session_id,
        }

    def init_payload(self) -> dict[str, Any]:
        router = self.runtime.router
        model_options = [
            {"provider": provider, "model": model}
            for provider in router.list_providers()
            for model in router.list_models(provider)
        ]
        return {
            "type": "init",
            "workspace": str(self._adapter.workspace),
            "approval_mode": self._adapter.approval_mode,
            "model_info": self.runtime.get_model_info(),
            "model_options": model_options,
            "history": self._history_messages(self.runtime.conversation_history),
            **self._status(),
        }

    @staticmethod
    def _history_messages(source: list[dict[str, Any]]) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        for msg in source:
            role = msg.get("role")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part.get("text", "") for part in content if part.get("type") == "text"
                )
            if not content:
                continue
            messages.append({"role": role, "content": str(content)})
        return messages

    @staticmethod
    def save_images(images: list[dict[str, Any]]) -> tuple[list[Path], list[str]]:
        paths: list[Path] = []
        rejected: list[str] = []
        for img in images:
            name = str(img.get("name") or "image.png")
            try:
                data = base64.b64decode(img.get("data") or "")
            except Exception:
                continue
            if not data:
                continue
            if len(data) > MAX_IMAGE_ATTACHMENT_BYTES:
                rejected.append(name)
                continue
            suffix = Path(name).suffix or ".png"
            with tempfile.NamedTemporaryFile(prefix="navi_web_", suffix=suffix, delete=False) as f:
                f.write(data)
                paths.append(Path(f.name))
        return paths, rejected

    def close(self) -> None:
        if self.busy:
            self.runtime.interrupt("Web 连接已断开")
        self._cancel_approval()
        self.queue.put_nowait(None)


class WebAdapter:
    """本地 Web UI 网关。默认只绑 127.0.0.1，URL 携带一次性 token。"""

    def __init__(
        self,
        workspace: str | Path = ".",
        host: str = "127.0.0.1",
        port: int = 8788,
        approval_mode: str = "normal",
        token: Optional[str] = None,
    ):
        if aiohttp is None:
            raise RuntimeError("aiohttp is required for the web gateway (pip install aiohttp)")
        self.workspace = Path(workspace).expanduser().resolve()
        self.host = host
        self.port = port
        self.approval_mode = approval_mode
        self.token = token or secrets.token_urlsafe(16)
        # kind -> {"task": Task | None, "account": str | None, "error": str | None}
        self._gateways: dict[str, dict[str, Any]] = {}
        # session_id -> AgentRuntime。构建 runtime 昂贵（含 MCP 发现），浏览器
        # 刷新/自动重连时按会话复用，与 qq.py 的 per-chat runtime 缓存同一模式。
        self._runtime_cache: dict[str, AgentRuntime] = {}

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/?token={self.token}"

    def create_runtime(self, resume_session_id, event_handler, approval_handler) -> AgentRuntime:
        return AgentRuntime(
            workspace=self.workspace,
            approval_mode=self.approval_mode,
            event_handler=event_handler,
            approval_handler=approval_handler,
            resume_session_id=resume_session_id,
            on_output=None,
            channel="cli",
        )

    def _authorized(self, request) -> bool:
        return secrets.compare_digest(request.query.get("token", ""), self.token)

    # ── HTTP handlers ────────────────────────────────────────────────────────

    async def handle_index(self, request):
        if not self._authorized(request):
            return web.Response(status=403, text="Forbidden: missing or invalid token")
        return web.FileResponse(WEBUI_DIR / "index.html")

    async def handle_sessions(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        sessions = HistoryStore.list_sessions(get_navi_home() / "history.sqlite3", limit=50)
        return web.json_response(sessions, dumps=_json_dumps)

    async def handle_ws(self, request):
        if not self._authorized(request):
            return web.Response(status=403, text="Forbidden")
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)

        resume_session_id = request.query.get("session") or None
        store = None
        if resume_session_id:
            # 历史读取很快，不应被后续 runtime/MCP 初始化阻塞。
            try:
                store = await asyncio.to_thread(
                    HistoryStore.from_existing,
                    get_navi_home() / "history.sqlite3",
                    resume_session_id,
                )
            except FileNotFoundError:
                pass
            else:
                await ws.send_str(
                    _json_dumps(
                        {
                            "type": "history",
                            "session_id": resume_session_id,
                            "history": WebConnection._history_messages(store.messages),
                        }
                    )
                )

        if store is not None:
            channel = str(store.meta.get("channel") or "").strip()
            if not channel:
                project_path = Path(store.meta.get("project_path") or "").resolve()
                navi_home = get_navi_home().resolve()
                if project_path == (navi_home / "qq" / "workspace").resolve():
                    channel = "qq"
                elif project_path == (navi_home / "weixin" / "workspace").resolve():
                    channel = "weixin"
            if channel in {"qq", "weixin"}:
                await ws.send_str(
                    _json_dumps(
                        {
                            "type": "read_only",
                            "session_id": resume_session_id,
                            "channel": channel,
                            "workspace": store.meta.get("project_path", ""),
                        }
                    )
                )
                updated_at = store.meta.get("updated_at")
                message_count = len(store.messages)
                while not ws.closed:
                    try:
                        msg = await ws.receive(timeout=1.0)
                    except asyncio.TimeoutError:
                        try:
                            refreshed = await asyncio.to_thread(
                                HistoryStore.from_existing,
                                get_navi_home() / "history.sqlite3",
                                resume_session_id,
                            )
                        except FileNotFoundError:
                            break
                        if (
                            refreshed.meta.get("updated_at") != updated_at
                            or len(refreshed.messages) != message_count
                        ):
                            updated_at = refreshed.meta.get("updated_at")
                            message_count = len(refreshed.messages)
                            await ws.send_str(
                                _json_dumps(
                                    {
                                        "type": "history",
                                        "session_id": resume_session_id,
                                        "history": WebConnection._history_messages(
                                            refreshed.messages
                                        ),
                                    }
                                )
                            )
                        continue
                    if msg.type in {
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.ERROR,
                    }:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await ws.send_str(
                            _json_dumps(
                                {"type": "notice", "text": "网关会话不可在 Web 中发送。"}
                            )
                        )
                return ws

        try:
            conn = await asyncio.to_thread(
                WebConnection, self, resume_session_id,
                asyncio.get_running_loop(),
            )
        except Exception as exc:
            logger.error("web: runtime init failed: %s", exc)
            with contextlib.suppress(Exception):
                await ws.send_str(_json_dumps({"type": "fatal", "error": f"初始化失败：{exc}"}))
                await ws.close()
            return ws

        async def send_events() -> None:
            while True:
                event = await conn.queue.get()
                if event is None:
                    return
                try:
                    await ws.send_str(_json_dumps(event))
                except ConnectionResetError:
                    return

        sender = asyncio.create_task(send_events())
        try:
            await ws.send_str(_json_dumps(conn.init_payload()))
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    data = json.loads(msg.data)
                except ValueError:
                    continue
                await self._dispatch(ws, conn, data)
        except ConnectionResetError:
            pass  # 浏览器在 init 发出前就走了（刷新/关闭），交给 finally 收尾
        finally:
            conn.close()
            with contextlib.suppress(asyncio.CancelledError):
                await sender
        return ws

    async def _dispatch(self, ws, conn: WebConnection, data: dict[str, Any]) -> None:
        mtype = data.get("type")
        if mtype == "user_message":
            if conn.busy:
                await ws.send_str(_json_dumps({"type": "notice", "text": "上一轮仍在进行中，请先等待或中断。"}))
                return
            text = str(data.get("text") or "").strip()
            image_paths, rejected_images = conn.save_images(data.get("images") or [])
            if rejected_images:
                names = "、".join(rejected_images[:3])
                if len(rejected_images) > 3:
                    names += f" 等 {len(rejected_images)} 张"
                error = f"图片超过 3MB，已拒绝上传：{names}"
                await ws.send_str(_json_dumps({"type": "notice", "text": error}))
                await ws.send_str(_json_dumps(conn._turn_end_payload({
                    "ok": False,
                    "error": error,
                    "final_answer": "",
                })))
                return
            if not text and not image_paths:
                return
            asyncio.create_task(conn.run_turn(text, image_paths))
        elif mtype == "approval_response":
            conn.resolve_approval(str(data.get("choice") or "reject"))
        elif mtype == "interrupt":
            conn.runtime.interrupt("用户通过 Web 请求取消")
        elif mtype == "switch_model":
            ok = conn.runtime.switch_model(
                str(data.get("provider") or ""), str(data.get("model") or "")
            )
            await ws.send_str(
                _json_dumps(
                    {
                        "type": "model_switched",
                        "ok": ok,
                        "model_info": conn.runtime.get_model_info(),
                    }
                )
            )

    # ── 网关托管（QQ / 微信跑在本进程的 asyncio 任务里）─────────────────────

    def _gateway_spec(self, kind: str):
        """返回 (list_accounts, make_adapter)，未知 kind 返回 None。惰性导入。"""
        navi_home_path = get_navi_home()
        navi_home = str(navi_home_path)
        if kind == "qq":
            from .qq import QqAdapter
            from .qqbot import list_qq_accounts

            workspace = navi_home_path / "qq" / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            return (
                lambda: list_qq_accounts(navi_home),
                lambda account: QqAdapter(account, workspace=workspace, approval_mode="open"),
            )
        if kind == "weixin":
            from .ilink import list_weixin_accounts
            from .weixin import WeixinAdapter

            workspace = navi_home_path / "weixin" / "workspace"
            workspace.mkdir(parents=True, exist_ok=True)
            return (
                lambda: list_weixin_accounts(navi_home),
                lambda account: WeixinAdapter(account, workspace=workspace, approval_mode="open"),
            )
        return None

    def _gateway_status(self, kind: str) -> dict[str, Any]:
        entry = self._gateways.get(kind) or {}
        task = entry.get("task")
        running = task is not None and not task.done()
        try:
            accounts = self._gateway_spec(kind)[0]()
        except Exception:
            accounts = []
        return {
            "accounts": accounts,
            "running": running,
            "account": entry.get("account") if running else None,
            "error": entry.get("error"),
        }

    def _on_gateway_done(self, kind: str, task) -> None:
        entry = self._gateways.get(kind)
        if entry is None or entry.get("task") is not task:
            return
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            entry["error"] = str(exc)
            logger.error("web: %s gateway crashed: %s", kind, exc)

    async def handle_gateways(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response(
            {kind: self._gateway_status(kind) for kind in ("qq", "weixin")},
            dumps=_json_dumps,
        )

    async def handle_gateway_start(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        kind = request.match_info["kind"]
        spec = self._gateway_spec(kind)
        if spec is None:
            return web.json_response({"error": f"未知网关：{kind}"}, status=404)
        entry = self._gateways.get(kind) or {}
        task = entry.get("task")
        if task is not None and not task.done():
            return web.json_response({"error": "网关已在运行。"}, status=409)
        try:
            body = await request.json()
        except Exception:
            body = {}
        list_accounts, make_adapter = spec
        try:
            accounts = list_accounts()
        except Exception:
            accounts = []
        account = str((body or {}).get("account") or "").strip()
        if not account and len(accounts) == 1:
            account = accounts[0]
        if not account or account not in accounts:
            return web.json_response(
                {"error": f"账号无效或未登录，请先在终端运行 navi {kind} login。"}, status=400
            )
        try:
            adapter = make_adapter(account)
        except Exception as exc:
            return web.json_response({"error": str(exc)}, status=500)
        gateway_task = asyncio.create_task(adapter.run())
        gateway_task.add_done_callback(lambda t: self._on_gateway_done(kind, t))
        self._gateways[kind] = {"task": gateway_task, "account": account, "error": None}
        logger.info("web: started %s gateway account=%s", kind, account)
        return web.json_response(self._gateway_status(kind), dumps=_json_dumps)

    async def handle_gateway_stop(self, request):
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        kind = request.match_info["kind"]
        entry = self._gateways.get(kind) or {}
        task = entry.get("task")
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            logger.info("web: stopped %s gateway", kind)
        return web.json_response(self._gateway_status(kind), dumps=_json_dumps)

    # ── 生命周期 ────────────────────────────────────────────────────────────

    def build_app(self):
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/ws", self.handle_ws)
        app.router.add_get("/api/sessions", self.handle_sessions)
        app.router.add_get("/api/gateways", self.handle_gateways)
        app.router.add_post("/api/gateways/{kind}/start", self.handle_gateway_start)
        app.router.add_post("/api/gateways/{kind}/stop", self.handle_gateway_stop)
        return app

    async def run(self) -> None:
        runner = web.AppRunner(self.build_app())
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info("web: serving on %s", self.url)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
