from __future__ import annotations

from nod.builtins import builtin_tools
from nod.messages import ToolCall
from nod.tools import ToolRegistry


def _registry() -> ToolRegistry:
    return ToolRegistry(builtin_tools())


def _cud_block(path: str, search: str, replacement: str) -> str:
    return (
        f"### FILE: {path}\n"
        "<<<<<<< SEARCH\n"
        f"{search}\n"
        "=======\n"
        f"{replacement}\n"
        ">>>>>>> REPLACE"
    )


def _delete_block(path: str) -> str:
    return f"### FILE: {path}\nDELETE"


def test_builtin_schemas_are_functions():
    schemas = _registry().schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"read_file", "cud_file", "list_dir", "run_shell", "http_get"}
    assert all(schema["type"] == "function" for schema in schemas)


def test_read_write_and_list_dir(tmp_path):
    registry = _registry()
    path = tmp_path / "nested" / "note.txt"

    written = registry.dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _cud_block(str(path), "", "hello")},
        )
    )
    assert written.startswith("created ")

    read = registry.dispatch(
        ToolCall(id="2", name="read_file", arguments={"path": str(path)})
    )
    assert read == "hello"

    listed = registry.dispatch(
        ToolCall(id="3", name="list_dir", arguments={"path": str(tmp_path)})
    )
    assert listed == "nested/"


def test_cud_file_create_existing_returns_error(tmp_path):
    path = tmp_path / "note.txt"
    path.write_text("already here", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _cud_block(str(path), "", "replacement")},
        )
    )

    assert out == f"error: {path}: file already exists"
    assert path.read_text(encoding="utf-8") == "already here"


def test_cud_file_updates_unique_search_match(tmp_path):
    path = tmp_path / "module.py"
    path.write_text(
        "def old_function():\n    return False\n",
        encoding="utf-8",
    )

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={
                "edits": _cud_block(
                    str(path),
                    "def old_function():\n    return False",
                    "def old_function():\n    return True",
                )
            },
        )
    )

    assert out.startswith("updated ")
    assert path.read_text(encoding="utf-8") == "def old_function():\n    return True\n"


def test_cud_file_missing_search_returns_error(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("actual contents", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _cud_block(str(path), "missing", "replacement")},
        )
    )

    assert out == f"error: {path}: SEARCH text not found"
    assert path.read_text(encoding="utf-8") == "actual contents"


def test_cud_file_ambiguous_search_requires_more_context(tmp_path):
    path = tmp_path / "module.py"
    path.write_text("same\nsame\n", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _cud_block(str(path), "same", "different")},
        )
    )

    assert out == (
        f"error: {path}: SEARCH text matched 2 times; "
        "add more surrounding context to make it unique"
    )
    assert path.read_text(encoding="utf-8") == "same\nsame\n"


def test_cud_file_deletes_existing_file(tmp_path):
    path = tmp_path / "dead.py"
    path.write_text("remove me", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _delete_block(str(path))},
        )
    )

    assert out.startswith("deleted ")
    assert not path.exists()


def test_cud_file_delete_missing_returns_error(tmp_path):
    path = tmp_path / "dead.py"

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="cud_file",
            arguments={"edits": _delete_block(str(path))},
        )
    )

    assert out == f"error: {path}: file does not exist"


def test_cud_file_best_effort_multi_block(tmp_path):
    created = tmp_path / "new.txt"
    updated = tmp_path / "update.txt"
    deleted = tmp_path / "dead.txt"
    existing = tmp_path / "existing.txt"
    updated.write_text("old value", encoding="utf-8")
    deleted.write_text("remove me", encoding="utf-8")
    existing.write_text("keep me", encoding="utf-8")

    edits = "\n".join(
        [
            _cud_block(str(created), "", "created contents"),
            _cud_block(str(updated), "old value", "new value"),
            _delete_block(str(deleted)),
            _cud_block(str(existing), "", "should fail"),
        ]
    )

    out = _registry().dispatch(
        ToolCall(id="1", name="cud_file", arguments={"edits": edits})
    )

    lines = out.splitlines()
    assert lines[0].startswith("created ")
    assert lines[1].startswith("updated ")
    assert lines[2].startswith("deleted ")
    assert lines[3] == f"error: {existing}: file already exists"
    assert created.read_text(encoding="utf-8") == "created contents"
    assert updated.read_text(encoding="utf-8") == "new value"
    assert not deleted.exists()
    assert existing.read_text(encoding="utf-8") == "keep me"


def test_cud_file_malformed_block_returns_error(tmp_path):
    path = tmp_path / "bad.txt"
    edits = f"### FILE: {path}\n<<<<<<< SEARCH\nold"

    out = _registry().dispatch(
        ToolCall(id="1", name="cud_file", arguments={"edits": edits})
    )

    assert out == f"error: {path}: missing ======= divider"


def test_read_file_missing_path_returns_error(tmp_path):
    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"path": str(tmp_path / "missing.txt")},
        )
    )
    assert out.startswith("error: could not read file")


def test_list_dir_missing_path_returns_error(tmp_path):
    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="list_dir",
            arguments={"path": str(tmp_path / "missing")},
        )
    )
    assert out.startswith("error: could not list directory")


def test_run_shell_returns_output_and_exit_code():
    out = _registry().dispatch(
        ToolCall(id="1", name="run_shell", arguments={"command": "printf hello"})
    )
    assert out.startswith("exit code: 0")
    assert "hello" in out


def test_http_get_uses_httpx_without_network(monkeypatch):
    class FakeResponse:
        status_code = 200
        reason_phrase = "OK"
        text = "body"

    calls = []

    def fake_get(url: str, timeout: int, follow_redirects: bool) -> FakeResponse:
        calls.append((url, timeout, follow_redirects))
        return FakeResponse()

    monkeypatch.setattr("nod.builtins.httpx.get", fake_get)
    out = _registry().dispatch(
        ToolCall(id="1", name="http_get", arguments={"url": "https://example.test"})
    )

    assert calls == [("https://example.test", 30, True)]
    assert out == "status: 200 OK\nbody"
