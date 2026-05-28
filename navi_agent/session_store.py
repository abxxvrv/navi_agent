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
        self.turns_path = self.session_dir / "turns.jsonl"
        self.events_path = self.session_dir / "events.jsonl"
        self.index_path = self.root / "index.jsonl"
        self.project_path = str(Path(project_path).resolve()) if project_path else str(Path.cwd().resolve())
        # 初始化内存状态
        self.created_at = self._now()

        self.turns: list[dict[str, Any]] = []
        self.event_id = 0
        # 初始化meta信息
        self.meta = {
            "session_id": self.session_id,
            "title": "Untitled session",
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "project_path": self.project_path,
            "turn_count": 0,
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
        instance.turns_path = session_dir / "turns.jsonl"
        instance.events_path = session_dir / "events.jsonl"
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

        # 加载 turns
        instance.turns = instance._read_jsonl(instance.turns_path)

        # 加载 event_id（取最大 event_id + 1）
        events = instance._read_jsonl(instance.events_path)
        max_id = 0
        for ev in events:
            eid = ev.get("event_id", 0)
            if isinstance(eid, int) and eid >= max_id:
                max_id = eid + 1
        instance.event_id = max_id

        return instance

    # 保存一轮用户输入 → agent 最终回答
    def append_turn(self, turn: dict[str, Any]) -> None:
        assistant = turn.get("final_answer") or turn.get("assistant") or ""
        semantic_turn = {
            "turn_id": turn.get("turn_id"),
            "created_at": turn.get("created_at") or self._now(),
            "user": turn.get("user", ""),
            "assistant": assistant,
        }
        
        # 同时写入内存和文件
        self.turns.append(semantic_turn)

        with self.turns_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(semantic_turn, ensure_ascii=False) + "\n")

        self.meta["updated_at"] = self._now()
        self.meta["turn_count"] = int(self.meta.get("turn_count", 0)) + 1
        
        # 如果标题还是默认标题，就用第一轮用户输入生成标题。
        if self.meta.get("title") == "Untitled session":
            self.meta["title"] = self._make_title(str(semantic_turn.get("user", "")))

        self._write_meta() # 我们前面只是修改了meta，但是还需要实际写入，所以用这个函数。
        self._update_index()
    
    # 添加事件记录
    def append_event(self, event: dict[str, Any]) -> None:
        event_record = {
            "event_id": self.event_id,
            "session_id": self.session_id,
            "created_at": self._now(),
            **event,
        }
        event_record = {
            key: value
            for key, value in event_record.items()
            if value is not None
        }

        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event_record, ensure_ascii=False) + "\n")

        self.event_id += 1
        self.meta["updated_at"] = event_record["created_at"]
        self._write_meta()
        self._update_index()
    
    def _stream_search_turns(
        self,
        turns_path: Path,
        keywords: list[str],
        context_chars: int = 300,
    ) -> list[tuple[int, dict[str, Any]]]:
        """逐行读 turns.jsonl，匹配的行截取关键词附近上下文后返回。"""
        if not turns_path.exists() or not turns_path.is_file():
            return []

        results: list[tuple[int, dict[str, Any]]] = []

        with turns_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                lower_line = line.lower()
                if not any(kw in lower_line for kw in keywords):
                    continue

                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(turn, dict):
                    continue

                score = 0
                # 完整查询串匹配权重高
                full_query = " ".join(keywords)
                if full_query in lower_line:
                    score += 10
                for kw in keywords:
                    if kw in lower_line:
                        score += 1

                if score <= 0:
                    continue

                user_text = str(turn.get("user", ""))
                assistant_text = str(turn.get("assistant", ""))

                # 截取匹配位置附近的上下文
                snippet_fields = {"user": user_text, "assistant": assistant_text}
                for field_name, field_text in snippet_fields.items():
                    if len(field_text) <= context_chars * 2:
                        continue
                    match_idx = -1
                    for kw in keywords:
                        match_idx = field_text.lower().find(kw)
                        if match_idx != -1:
                            break
                    if match_idx == -1:
                        field_text = field_text[: context_chars * 2]
                    else:
                        start = max(0, match_idx - context_chars)
                        end = min(len(field_text), match_idx + context_chars)
                        field_text = field_text[start:end]
                        if start > 0:
                            field_text = "..." + field_text
                        if end < len(turn.get(field_name, "")):
                            field_text = field_text + "..."
                    snippet_fields[field_name] = field_text

                item = {
                    "turn_id": turn.get("turn_id"),
                    "created_at": turn.get("created_at"),
                    "user": snippet_fields["user"],
                    "final_answer": snippet_fields["assistant"],
                    "source": "turns.jsonl",
                }

                results.append((score, item))

        return results

    def _stream_search_events(
        self,
        events_path: Path,
        keywords: list[str],
        context_chars: int = 300,
    ) -> list[tuple[int, dict[str, Any]]]:
        """逐行读 events.jsonl，按 turn_id 分组后匹配，截取上下文。"""
        if not events_path.exists() or not events_path.is_file():
            return []

        # 第一遍：逐行读，按 turn_id 分组（只存匹配的 turn）
        matched_turns: dict[int, list[dict[str, Any]]] = {}
        with events_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue

                lower_line = line.lower()
                if not any(kw in lower_line for kw in keywords):
                    continue

                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if not isinstance(event, dict):
                    continue

                turn_id = event.get("turn_id")
                if isinstance(turn_id, int):
                    matched_turns.setdefault(turn_id, []).append(event)

        # 第二遍：对匹配的 turn 做评分和截取
        results: list[tuple[int, dict[str, Any]]] = []
        full_query = " ".join(keywords)

        for turn_id, events in matched_turns.items():
            user = ""
            final_answer = ""
            created_at = ""

            for event in events:
                event_type = event.get("type")
                if not created_at:
                    created_at = str(event.get("created_at") or "")
                if event_type == "turn_start":
                    user = str(event.get("user") or user)
                elif event_type == "turn_end":
                    final_answer = str(event.get("final_answer") or final_answer)
                    created_at = str(event.get("created_at") or created_at)

            searchable = f"{user}\n{final_answer}".lower()
            score = 0
            if full_query in searchable:
                score += 10
            for kw in keywords:
                if kw in searchable:
                    score += 1

            if score <= 0:
                continue

            # 截取
            for field_name, field_text in [("user", user), ("final_answer", final_answer)]:
                if len(field_text) <= context_chars * 2:
                    continue
                original_len = len(field_text)
                match_idx = -1
                for kw in keywords:
                    match_idx = field_text.lower().find(kw)
                    if match_idx != -1:
                        break
                if match_idx == -1:
                    field_text = field_text[: context_chars * 2]
                else:
                    start = max(0, match_idx - context_chars)
                    end = min(original_len, match_idx + context_chars)
                    field_text = field_text[start:end]
                    if start > 0:
                        field_text = "..." + field_text
                    if end < original_len:
                        field_text = field_text + "..."
                if field_name == "user":
                    user = field_text
                else:
                    final_answer = field_text

            item = {
                "turn_id": turn_id,
                "created_at": created_at,
                "user": user,
                "final_answer": final_answer,
                "source": "events.jsonl",
            }

            results.append((score, item))

        return results

    # 全部会话搜索工具
    def search(
        self,
        query: str,
        limit: int = 5,
        include_trace: bool = False,
        context_chars: int = 300,
    ) -> list[dict[str, Any]]:
        query = query.strip().lower()
        if not query:
            return []

        if limit < 1:
            limit = 1

        if limit > 20:
            limit = 20

        keywords = query.split()
        scored: list[tuple[int, str, dict[str, Any]]] = []

        # 1. 找到所有 session 目录
        # 优先用 index.jsonl，因为它是 session 索引。
        # 同时再扫描 root 目录兜底，避免 index.jsonl 不完整。
        session_dirs: list[Path] = []
        seen_session_ids: set[str] = set()

        for item in self._read_jsonl(self.index_path):
            session_id = item.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue

            session_dir = self.root / session_id

            if not session_dir.exists() or not session_dir.is_dir():
                continue

            if session_id in seen_session_ids:
                continue

            session_dirs.append(session_dir)
            seen_session_ids.add(session_id)

        # 兜底扫描 sessions 根目录
        if self.root.exists() and self.root.is_dir():
            for item in sorted(self.root.iterdir(), reverse=True):
                if not item.is_dir():
                    continue

                session_id = item.name
                if session_id in seen_session_ids:
                    continue

                session_dirs.append(item)
                seen_session_ids.add(session_id)

        # 2. 遍历每个 session
        for session_dir in session_dirs:
            meta_path = session_dir / "meta.json"
            turns_path = session_dir / "turns.jsonl"
            events_path = session_dir / "events.jsonl"

            session_meta: dict[str, Any] = {}

            if meta_path.exists() and meta_path.is_file():
                try:
                    loaded_meta = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(loaded_meta, dict):
                        session_meta = loaded_meta
                except (json.JSONDecodeError, UnicodeDecodeError):
                    session_meta = {}

            session_id = str(session_meta.get("session_id") or session_dir.name)
            session_title = str(session_meta.get("title") or "")
            project_path = str(session_meta.get("project_path") or "")

            # 3A. include_trace=True：流式搜 events.jsonl
            if include_trace:
                results = self._stream_search_events(events_path, keywords, context_chars)
                for score, item in results:
                    item["session_id"] = session_id
                    item["session_title"] = session_title
                    item["project_path"] = project_path
                    sort_time = str(
                        item.get("created_at")
                        or session_meta.get("updated_at")
                        or session_meta.get("created_at")
                        or ""
                    )
                    scored.append((score, sort_time, item))

                continue

            # 3B. include_trace=False：流式搜 turns.jsonl
            results = self._stream_search_turns(turns_path, keywords, context_chars)
            for score, item in results:
                item["session_id"] = session_id
                item["session_title"] = session_title
                item["project_path"] = project_path
                sort_time = str(
                    item.get("created_at")
                    or session_meta.get("updated_at")
                    or session_meta.get("created_at")
                    or ""
                )
                scored.append((score, sort_time, item))

        # 4. 排序：先按匹配分数，再按时间
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)

        return [item for _, _, item in scored[:limit]]

    def _write_meta(self) -> None:
        self.meta_path.write_text(
            json.dumps(self.meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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

    # 辅助全部会话搜索工具的一个函数，会使用 read_jsonl ，返回按照 轮次 id 分组的 JSON
    def _load_events_by_turn(
        self,
        events_path: Path | None = None,
    ) -> dict[int, list[dict[str, Any]]]:
        events_by_turn: dict[int, list[dict[str, Any]]] = {}

        path = events_path or self.events_path

        for event in self._read_jsonl(path):
            turn_id = event.get("turn_id")
            if isinstance(turn_id, int):
                events_by_turn.setdefault(turn_id, []).append(event)

        return events_by_turn
    
    def _make_title(self, user: str) -> str:
        title = " ".join(user.strip().split())
        return title[:40] if title else "Untitled session"

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

