"""手动验证 file_lock 是否真的生效（一次性脚本，可随时删除）。

跑法： python manual_lock_check.py
"""
import os
import tempfile
import threading
import time
from pathlib import Path

# 用临时目录当 NAVI_HOME，避免动到真实 ~/.navi
os.environ["NAVI_HOME"] = tempfile.mkdtemp(prefix="navi_lock_demo_")

from navi_agent.storage.safe_file import file_lock, _lock_path, FileLockTimeout

target = Path(os.environ["NAVI_HOME"]) / "data.txt"
out = threading.Lock()
intervals = []


def worker(name):
    with file_lock(target):
        t_in = time.monotonic()
        with out:
            print(f"  [{name}] 拿到锁 @ {t_in:.3f}")
        time.sleep(0.5)                      # 持锁 0.5 秒
        t_out = time.monotonic()
        with out:
            print(f"  [{name}] 放锁   @ {t_out:.3f}")
        intervals.append((t_in, t_out))


print("=== 测试1：两个线程抢同一文件的锁，应被串行 ===")
a = threading.Thread(target=worker, args=("线程A",))
b = threading.Thread(target=worker, args=("线程B",))
start = time.monotonic()
a.start(); b.start(); a.join(); b.join()
total = time.monotonic() - start

(i1, o1), (i2, o2) = sorted(intervals)
overlap = o1 > i2
print(f"  总耗时 {total:.2f}s （串行≈1.0s，并行≈0.5s）")
print("  结论:", "❌ 锁没生效（区间重叠）" if overlap else "✅ 锁生效，两次写被串行")

print("\n=== 测试2：锁被占用时，另一个获取应超时报错 ===")
def hold():
    with file_lock(target):
        time.sleep(1.0)
h = threading.Thread(target=hold); h.start()
time.sleep(0.1)                              # 让它先拿到锁
try:
    with file_lock(target, timeout=0.3):
        print("  ❌ 不该拿到锁")
except FileLockTimeout:
    print("  ✅ 0.3s 内拿不到 → 正确抛 FileLockTimeout")
h.join()

print("\n=== 测试3：锁文件随持锁出现 / 释放后消失 ===")
lp = _lock_path(target)
print("  拿锁前 锁文件存在:", lp.exists())
with file_lock(target):
    print("  持锁中 锁文件存在:", lp.exists())
print("  放锁后 锁文件存在:", lp.exists())
