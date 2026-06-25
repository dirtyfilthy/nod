from __future__ import annotations

import json

from nod.memory import InMemoryMemory, JSONFileMemory
from nod.messages import assistant, system, user


def test_json_file_memory_round_trip_assigns_ids(tmp_path):
    memory = JSONFileMemory(tmp_path)
    messages = [user("hi"), assistant("hello")]

    memory.persist_messages("s1", messages)
    loaded = memory.load("s1")

    assert [m.content for m in loaded] == ["hi", "hello"]
    assert all(m.id for m in loaded)
    assert messages[0].id == loaded[0].id


def test_json_file_memory_append_is_idempotent_for_loaded_messages(tmp_path):
    memory = JSONFileMemory(tmp_path)
    messages = [user("hi")]
    memory.persist_messages("s1", messages)
    memory.persist_messages("s1", messages)

    path = tmp_path / "s1.jsonl"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_json_file_memory_load_resolves_latest_compaction(tmp_path):
    memory = JSONFileMemory(tmp_path)
    messages = [user("old"), assistant("done"), user("recent")]
    memory.persist_messages("s1", messages)
    compaction_id = memory.persist_compaction(
        "s1", "summary", messages[2].id or "", tokens_before=100
    )

    loaded = memory.load("s1")

    assert [m.kind for m in loaded] == ["compaction_summary", "message"]
    assert [m.content for m in loaded] == ["summary", "recent"]
    assert loaded[0].id == compaction_id


def test_json_file_memory_migrates_old_json_array(tmp_path):
    legacy = tmp_path / "old.json"
    legacy.write_text(
        json.dumps([system("be nod").to_storage_dict(), user("hi").to_storage_dict()]),
        encoding="utf-8",
    )
    memory = JSONFileMemory(tmp_path)

    loaded = memory.load("old")

    assert [m.content for m in loaded] == ["hi"]
    assert (tmp_path / "old.jsonl").exists()
    assert (tmp_path / "old.json.migrated").exists()


def test_in_memory_memory_resolves_compaction():
    memory = InMemoryMemory()
    messages = [user("old"), user("recent")]
    memory.persist_messages("s1", messages)
    memory.persist_compaction("s1", "summary", messages[1].id or "", 100)

    loaded = memory.load("s1")

    assert [m.content for m in loaded] == ["summary", "recent"]
