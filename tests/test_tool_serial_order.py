"""同一真实路径的写操作按模型顺序串行、其余并行的门闩机制测试。"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from navi_agent.runtime.agent import AgentRuntime
from navi_agent.runtime.interrupt import clear_all


class _RecordingRegistry:
    def __init__(self):
        self.order = []
        self._lock = threading.Lock()

    def invoke(self, _name, args):
        with self._lock:
            self.order.append(args["cid"])
        time.sleep(0.05)  # 拉长执行窗口，暴露潜在乱序
        return {"ok": True}


def _make_runtime(registry):
    rt = AgentRuntime.__new__(AgentRuntime)
    rt.event_handler = None
    rt.cancel_event = threading.Event()
    rt._tool_worker_threads = set()
    rt._tool_worker_threads_lock = threading.Lock()
    rt.tool_registry = registry
    rt.session_store = SimpleNamespace(session_id="session")
    rt.hooks = SimpleNamespace(dispatch=lambda *_args, **_kwargs: None)
    return rt


def _build_gates(to_execute):
    """复刻 _tool_node 的门闩构建（此处所有 cid 视为同一真实路径）。"""
    wait_on = {}
    done_signal = {}
    prev = None
    for cid, _, _ in to_execute:
        if prev is not None:
            wait_on[cid] = done_signal.setdefault(prev, threading.Event())
        prev = cid
    return wait_on, done_signal


def test_same_file_writes_execute_in_model_order():
    clear_all()
    reg = _RecordingRegistry()
    rt = _make_runtime(reg)

    to_execute = [
        ("A", "write_file", {"cid": "A"}),
        ("B", "write_file", {"cid": "B"}),
        ("C", "write_file", {"cid": "C"}),
    ]
    wait_on, done_signal = _build_gates(to_execute)

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [
            ex.submit(
                rt._execute_single_tool_ordered, name, args, cid,
                wait_on.get(cid), done_signal.get(cid),
            )
            for cid, name, args in to_execute
        ]
        for f in futures:
            f.result()

    assert reg.order == ["A", "B", "C"]


def test_independent_writes_run_in_parallel():
    clear_all()
    barrier = threading.Barrier(3, timeout=2)

    class _BarrierRegistry:
        def invoke(self, _name, _args):
            # 只有三者真正并行才能同时通过 barrier；若被串行化会超时报错
            barrier.wait()
            return {"ok": True}

    rt = _make_runtime(_BarrierRegistry())
    # 三个不同文件 → 无门闩，全部并行
    to_execute = [("A", "write_file", {}), ("B", "write_file", {}), ("C", "write_file", {})]

    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = [
            ex.submit(rt._execute_single_tool_ordered, name, args, cid, None, None)
            for cid, name, args in to_execute
        ]
        results = [f.result() for f in futures]

    assert all(r[1]["ok"] for r in results)


def test_done_event_set_after_run():
    clear_all()
    reg = _RecordingRegistry()
    rt = _make_runtime(reg)

    done = threading.Event()
    rt._execute_single_tool_ordered("write_file", {"cid": "A"}, "A", None, done)

    assert done.is_set()
    assert reg.order == ["A"]


def test_waiter_bails_out_when_cancelled():
    clear_all()
    reg = _RecordingRegistry()
    rt = _make_runtime(reg)
    rt.cancel_event.set()  # 标记为已取消

    never = threading.Event()  # 永不触发：未取消时会一直阻塞
    result = rt._execute_single_tool_ordered("write_file", {"cid": "B"}, "B", never, None)

    assert result[0] == "B"
    assert result[1]["ok"] is False
    assert "跳过" in result[1]["error"]
    assert reg.order == []  # 前序未完成 → 不应真正执行工具
