"""Memory: where a conversation persists between turns.

The `Memory` interface is deliberately tiny -- load a session's messages, save
them back. The default writes one JSON file per session. Swap in a database or a
vector-recall store later by passing any object with the same two methods.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Protocol

from nod.messages import Message


class Memory(Protocol):
    def load(self, session_id: str) -> list[Message]: ...

    def save(self, session_id: str, messages: list[Message]) -> None: ...


class InMemoryMemory:
    """Non-persistent store, handy for tests and one-off runs."""

    def __init__(self) -> None:
        self._store: dict[str, list[Message]] = {}

    def load(self, session_id: str) -> list[Message]:
        return list(self._store.get(session_id, []))

    def save(self, session_id: str, messages: list[Message]) -> None:
        self._store[session_id] = list(messages)


class JSONFileMemory:
    """One JSON file per session under `root`."""

    def __init__(self, root: Path | str = "~/.nod/memory") -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "default"
        return self.root / f"{safe}.json"

    def load(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [Message.from_storage_dict(m) for m in raw]

    def save(self, session_id: str, messages: list[Message]) -> None:
        path = self._path(session_id)
        data = [m.to_storage_dict() for m in messages]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
