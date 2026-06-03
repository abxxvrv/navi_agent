import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


class SessionStore:
    def __init__( # 初始化
        self,
        root: str = ".light_agent/sessions", # 默认保存的位置
        project_path: str | None = None,
    ):
        # 创建目录，也就是".light_agent/sessions"
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

        self.session_id = ( # 创建会话ID
            datetime.now().strftime("%Y%m%d_%H%M%S")
            + "_"
            + uuid.uuid4().hex[:8]
        )
        self.session_dir = self.root / self.session_id
        self.path = self.session_dir
        self.meta_path = self.session_dir / "meta.json"
        self.messages_path = self.session_dir / "messages.jsonl"
        self.index_path = self.root / "index.jsonl"
        self.project_path = str(Path(project_path).resolve()) if project_path else str(Path.cwd().resolve())
        # 初始化内存状态
        self.created_at = self._now()
        self.messages: list[dict[str, Any]] = []

        # 初始化meta信息
        self.meta = {
            "session_id": self.session_id,
            "title": "Untitled session",
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "project_path": self.project_path,
        }
        # 创建 session 目录并写入初始文件
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._write_meta()
        self._update_index()

    @classmethod
    def from_existing(cls, session_dir: str | Path, root: str | None = None) -> "SessionStore":
        """从已有 session 目录加载，不创建新目录或写 index。"""
        session_dir = Path(session_dir).resolve()
        if root is None:
            root = str(session_dir.parent)

        instance = cls.__new__(cls)
        instance.root = Path(root)
        instance.session_dir = session_dir
        instance.path = session_dir
        instance.meta_path = session_dir / "meta.json"
        instance.messages_path = session_dir / "messages.jsonl"
        instance.index_path = instance.root / "index.jsonl"

        # 加载 meta
        instance.meta = {}
        if instance.meta_path.exists():
            try:
                instance.meta = json.loads(instance.meta_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                instance.meta = {}

        instance.session_id = instance.meta.get("session_id", session_dir.name)
        instance.project_path = instance.meta.get("project_path", "")
        instance.created_at = instance.meta.get("created_at", "")
        instance.messages = instance._read_jsonl(instance.messages_path)

        return instance

    def append_message(self, message: dict[str, Any]) -> None:
        record = {
            key: value
            for key, value in message.items()
            if value is not None
        }

        self.messages.append(record)

        with self.messages_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        self.meta["updated_at"] = self._now()
        if record.get("role") == "user" and self.meta.get("title") == "Untitled session":
            self.meta["title"] = self._make_title(str(record.get("content", "")))

        self._write_meta()
        self._update_index()

    def _write_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_usage(self, usage: dict[str, int]) -> None:
        self.meta["last_usage"] = usage
        self._write_meta()

    def get_usage(self) -> dict[str, int]:
        raw = self.meta.get("last_usage", {})
        return {k: v for k, v in raw.items() if k in ("prompt_tokens", "completion_tokens")}

    def _update_index(self) -> None:
        rows: list[dict[str, Any]] = []

        if self.index_path.exists():
            for line in self.index_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if item.get("session_id") != self.session_id:
                    rows.append(item)

        rows.append(dict(self.meta))

        with self.index_path.open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")

    # 读取 jsonl 文件，相当于把每一行的 jsonl 内容转成 json
    def _read_jsonl(self, path: Path) -> list[dict[str, Any]]:
    # 读取 jsonl 文件。

    # 每一行应该是一个 JSON object。
    # 空行跳过。
    # 解析失败的行跳过。
    # 文件不存在时返回空列表。
        if not path.exists() or not path.is_file():
            return []

        rows: list[dict[str, Any]] = []

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return []

        for line in lines:
            line = line.strip()
            if not line:
                continue

            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue

            if isinstance(item, dict):
                rows.append(item)

        return rows

    def _make_title(self, user: str) -> str:
        title = " ".join(user.strip().split())
        return title[:40] if title else "Untitled session"

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

