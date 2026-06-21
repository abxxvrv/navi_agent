from __future__ import annotations

import hashlib
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator

from ..paths import get_navi_home


@dataclass(frozen=True)
class FileVersion:
    exists: bool
    sha256: str
    mtime_ns: int
    size: int

    def to_dict(self) -> dict:
        return asdict(self)


class FileLockTimeout(TimeoutError):
    pass


def file_version(path: Path, prev: FileVersion | None = None) -> FileVersion:
    path = path.resolve()
    if not path.exists():
        return FileVersion(exists=False, sha256="", mtime_ns=0, size=0)

    stat = path.stat()

    # 快路径：prev 存在且 mtime_ns+size 都吻合 → 复用 prev（跳过哈希）
    if (
        prev is not None
        and prev.exists
        and stat.st_mtime_ns == prev.mtime_ns
        and stat.st_size == prev.size
    ):
        return prev

    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)

    return FileVersion(
        exists=True,
        sha256=digest.hexdigest(),
        mtime_ns=stat.st_mtime_ns,
        size=stat.st_size,
    )


def atomic_write_text(path: Path, content: str, encoding: str = "utf-8") -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    mode = None
    if path.exists():
        mode = path.stat().st_mode

    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=str(path.parent),
        text=True,
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        if mode is not None:
            os.chmod(tmp_path, mode)

        os.replace(tmp_path, path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _lock_path(target: Path) -> Path:
    key = hashlib.sha256(str(target.resolve()).encode("utf-8")).hexdigest()
    locks_dir = get_navi_home() / "locks" / "files"
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"{key}.lock"


@contextmanager
def file_lock(
    target: Path,
    *,
    timeout: float = 10.0,
    poll_interval: float = 0.05,
    stale_after: float = 300.0,
) -> Iterator[None]:
    lock_path = _lock_path(target)
    started = time.monotonic()
    token = f"pid={os.getpid()} time={time.time()} path={target.resolve()}\n"

    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(token)
            break
        except FileExistsError:
            try:
                age = time.time() - lock_path.stat().st_mtime
                if age > stale_after:
                    lock_path.unlink()
                    continue
            except FileNotFoundError:
                continue
            except OSError:
                pass

            if time.monotonic() - started >= timeout:
                raise FileLockTimeout(f"写入锁等待超时: {target}")
            time.sleep(poll_interval)

    try:
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
