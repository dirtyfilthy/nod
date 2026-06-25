"""Memory: where a conversation persists between turns.

The default memory is append-only JSONL: every message and compaction checkpoint
is appended as a new event. Loading a session resolves the latest compaction into
a compact working context without deleting earlier history.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from nod.messages import Message


class Memory(Protocol):
    def load(self, session_id: str) -> list[Message]: ...

    def persist_messages(self, session_id: str, messages: list[Message]) -> None: ...

    def persist_compaction(
        self, session_id: str, summary: str, first_kept_entry_id: str, tokens_before: int
    ) -> str: ...


@dataclass
class _Entry:
    id: str
    type: str
    message: Message | None = None
    summary: str | None = None
    first_kept_entry_id: str | None = None
    tokens_before: int | None = None

    def to_dict(self) -> dict[str, Any]:
        if self.type == "message":
            if self.message is None:
                raise ValueError("message entry missing message")
            data = self.message.to_storage_dict()
            data["id"] = self.id
            data["type"] = "message"
            return data
        if self.type == "compaction":
            return {
                "id": self.id,
                "type": "compaction",
                "summary": self.summary or "",
                "first_kept_entry_id": self.first_kept_entry_id or "",
                "tokens_before": self.tokens_before or 0,
            }
        raise ValueError(f"unknown entry type: {self.type}")


def _message_entry(message: Message) -> _Entry:
    entry_id = message.id or uuid.uuid4().hex
    message.id = entry_id
    return _Entry(id=entry_id, type="message", message=message)


def _entry_from_dict(raw: dict[str, Any]) -> _Entry | None:
    entry_id = raw.get("id") or uuid.uuid4().hex
    entry_type = raw.get("type", "message")
    if entry_type == "message":
        data = dict(raw)
        data["id"] = entry_id
        data.setdefault("kind", "message")
        return _Entry(id=entry_id, type="message", message=Message.from_storage_dict(data))
    if entry_type == "compaction":
        return _Entry(
            id=entry_id,
            type="compaction",
            summary=raw.get("summary", "") or "",
            first_kept_entry_id=raw.get("first_kept_entry_id", "") or "",
            tokens_before=int(raw.get("tokens_before", 0) or 0),
        )
    return None


def _resolve_entries(entries: list[_Entry]) -> list[Message]:
    last_compaction_index = -1
    for i, entry in enumerate(entries):
        if entry.type == "compaction":
            last_compaction_index = i

    if last_compaction_index == -1:
        return [
            entry.message
            for entry in entries
            if entry.type == "message"
            and entry.message is not None
            and entry.message.role != "system"
        ]

    compaction = entries[last_compaction_index]
    first_kept_index = next(
        (
            i
            for i, entry in enumerate(entries)
            if entry.id == compaction.first_kept_entry_id and entry.type == "message"
        ),
        last_compaction_index + 1,
    )
    summary = Message(
        role="user",
        content=compaction.summary or "",
        id=compaction.id,
        kind="compaction_summary",
    )
    kept = [
        entry.message
        for entry in entries[first_kept_index:]
        if entry.type == "message" and entry.message is not None and entry.message.role != "system"
    ]
    return [summary, *kept]


class InMemoryMemory:
    """Non-persistent store, handy for tests and one-off runs."""

    def __init__(self) -> None:
        self._entries: dict[str, list[_Entry]] = {}
        self._persisted_ids: dict[str, set[str]] = {}

    def load(self, session_id: str) -> list[Message]:
        entries = self._entries.get(session_id, [])
        self._persisted_ids[session_id] = {entry.id for entry in entries}
        return list(_resolve_entries(entries))

    def persist_messages(self, session_id: str, messages: list[Message]) -> None:
        entries = self._entries.setdefault(session_id, [])
        persisted = self._persisted_ids.setdefault(session_id, {entry.id for entry in entries})
        for message in messages:
            if message.kind == "compaction_summary":
                continue
            if message.id is not None and message.id in persisted:
                continue
            entry = _message_entry(message)
            entries.append(entry)
            persisted.add(entry.id)

    def persist_compaction(
        self, session_id: str, summary: str, first_kept_entry_id: str, tokens_before: int
    ) -> str:
        entry_id = uuid.uuid4().hex
        self._entries.setdefault(session_id, []).append(
            _Entry(
                id=entry_id,
                type="compaction",
                summary=summary,
                first_kept_entry_id=first_kept_entry_id,
                tokens_before=tokens_before,
            )
        )
        self._persisted_ids.setdefault(session_id, set()).add(entry_id)
        return entry_id

    def save(self, session_id: str, messages: list[Message]) -> None:
        """Compatibility shim for older callers."""
        self.persist_messages(session_id, messages)


class JSONFileMemory:
    """Append-only JSONL session files under `root`."""

    def __init__(self, root: Path | str = "~/.nod/memory") -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._persisted_ids: dict[str, set[str]] = {}

    def _path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "default"
        return self.root / f"{safe}.jsonl"

    def _legacy_path(self, session_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id) or "default"
        return self.root / f"{safe}.json"

    def load(self, session_id: str) -> list[Message]:
        path = self._path(session_id)
        if not path.exists():
            self._migrate_legacy(session_id)
        if not path.exists():
            return []
        entries = self._read_entries(path)
        self._persisted_ids[session_id] = {entry.id for entry in entries}
        return _resolve_entries(entries)

    def persist_messages(self, session_id: str, messages: list[Message]) -> None:
        path = self._path(session_id)
        persisted = self._persisted_ids.setdefault(session_id, self._load_ids(path))
        lines: list[str] = []
        for message in messages:
            if message.kind == "compaction_summary":
                continue
            if message.id is not None and message.id in persisted:
                continue
            entry = _message_entry(message)
            lines.append(json.dumps(entry.to_dict(), separators=(",", ":")))
            persisted.add(entry.id)
        if lines:
            with path.open("a", encoding="utf-8") as f:
                for line in lines:
                    f.write(line)
                    f.write("\n")

    def persist_compaction(
        self, session_id: str, summary: str, first_kept_entry_id: str, tokens_before: int
    ) -> str:
        path = self._path(session_id)
        persisted = self._persisted_ids.setdefault(session_id, self._load_ids(path))
        entry = _Entry(
            id=uuid.uuid4().hex,
            type="compaction",
            summary=summary,
            first_kept_entry_id=first_kept_entry_id,
            tokens_before=tokens_before,
        )
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry.to_dict(), separators=(",", ":")))
            f.write("\n")
        persisted.add(entry.id)
        return entry.id

    def save(self, session_id: str, messages: list[Message]) -> None:
        """Compatibility shim for older callers."""
        self.persist_messages(session_id, messages)

    def _read_entries(self, path: Path) -> list[_Entry]:
        entries: list[_Entry] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                return []
            if isinstance(raw, dict):
                entry = _entry_from_dict(raw)
                if entry is not None:
                    entries.append(entry)
        return entries

    def _load_ids(self, path: Path) -> set[str]:
        return {entry.id for entry in self._read_entries(path)} if path.exists() else set()

    def _migrate_legacy(self, session_id: str) -> None:
        legacy = self._legacy_path(session_id)
        path = self._path(session_id)
        if not legacy.exists() or path.exists():
            return
        try:
            raw = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return
        entries: list[_Entry] = []
        for item in raw:
            if not isinstance(item, dict):
                return
            entries.append(_message_entry(Message.from_storage_dict(item)))
        with path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry.to_dict(), separators=(",", ":")))
                f.write("\n")
        try:
            legacy.rename(legacy.with_suffix(".json.migrated"))
        except OSError:
            pass

    def _legacy_save(self, session_id: str, messages: list[Message]) -> None:
        path = self._path(session_id)
        data = [m.to_storage_dict() for m in messages]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
