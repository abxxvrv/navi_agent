"""Agent 实例存储 — 管理子 agent 的持久化和恢复。"""

from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from ..paths import get_agents_dir


class AgentInstanceStore:
    """管理子 agent 实例的生命周期存储。

    目录结构：
        ~/.navi/agents/<agent_id>/
            meta.json       # 实例元信息
            context.jsonl   # 对话历史
    """

    def __init__(self, root: Path | None = None):
        self.root = root or get_agents_dir()
        self._lock = threading.RLock()

    def create(
        self,
        agent_type: str = "default",
        system_prompt: str | None = None,
        tool_names: list[str] | None = None,
    ) -> str:
        """创建新实例，返回 agent_id。"""
        agent_id = f"a_{uuid.uuid4().hex[:8]}"
        instance_dir = self.root / agent_id
        instance_dir.mkdir(parents=True, exist_ok=True)

        meta = {
            "agent_id": agent_id,
            "agent_type": agent_type,
            "status": "idle",
            "created_at": time.time(),
            "updated_at": time.time(),
            "system_prompt": system_prompt,
            "tool_names": tool_names or [],
        }
        (instance_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (instance_dir / "context.jsonl").touch()
        return agent_id

    def get_meta(self, agent_id: str) -> dict[str, Any] | None:
        """读取实例元信息，不存在返回 None。"""
        with self._lock:
            meta_path = self.root / agent_id / "meta.json"
            if not meta_path.exists():
                return None
            return json.loads(meta_path.read_text(encoding="utf-8"))

    def update_meta(self, agent_id: str, **fields: Any) -> None:
        """更新实例元信息的指定字段。"""
        with self._lock:
            meta = self.get_meta(agent_id)
            if meta is None:
                return
            meta.update(fields)
            meta["updated_at"] = time.time()
            meta_path = self.root / agent_id / "meta.json"
            meta_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def load_context(self, agent_id: str) -> list[dict[str, Any]]:
        """加载实例的对话历史。"""
        context_path = self.root / agent_id / "context.jsonl"
        if not context_path.exists():
            return []
        messages = []
        for line in context_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                messages.append(json.loads(line))
        return messages

    def save_context(self, agent_id: str, messages: list[dict[str, Any]]) -> None:
        """保存对话历史到实例。"""
        context_path = self.root / agent_id / "context.jsonl"
        lines = [json.dumps(m, ensure_ascii=False) for m in messages]
        context_path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")

    def list_instances(self) -> list[dict[str, Any]]:
        """列出所有实例的元信息。"""
        instances = []
        if not self.root.exists():
            return instances
        for path in sorted(self.root.iterdir()):
            if not path.is_dir():
                continue
            meta = self.get_meta(path.name)
            if meta:
                instances.append(meta)
        return instances

    def delete(self, agent_id: str) -> bool:
        """删除实例，返回是否成功。"""
        instance_dir = self.root / agent_id
        if not instance_dir.exists():
            return False
        shutil.rmtree(instance_dir)
        return True
