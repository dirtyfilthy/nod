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
        ToolCall(
            id="2",
            name="read_file",
            arguments={"filename": str(path), "start_line": 0, "line_count": 10},
        )
    )
    assert read == (
        "start_offset: 0\n"
        "lines_returned: 1\n"
        "end_offset: 1\n"
        "next_offset: 1\n"
        "total_lines: 1\n"
        "\n"
        "hello"
    )

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
            arguments={
                "filename": str(tmp_path / "missing.txt"),
                "start_line": 0,
                "line_count": 1,
            },
        )
    )
    assert out.startswith("error: could not read file")


def test_read_file_returns_requested_line_range_with_metadata(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("zero\none\ntwo\nthree\n", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 1, "line_count": 2},
        )
    )

    assert out == (
        "start_offset: 1\n"
        "lines_returned: 2\n"
        "end_offset: 3\n"
        "next_offset: 3\n"
        "total_lines: 4\n"
        "\n"
        "one\n"
        "two"
    )


def test_read_file_short_read_past_end_is_not_an_error(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("zero\none\n", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 1, "line_count": 10},
        )
    )

    assert out == (
        "start_offset: 1\n"
        "lines_returned: 1\n"
        "end_offset: 2\n"
        "next_offset: 2\n"
        "total_lines: 2\n"
        "\n"
        "one"
    )


def test_read_file_start_past_end_returns_only_header(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("zero\none\n", encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 10, "line_count": 5},
        )
    )

    assert out == (
        "start_offset: 10\n"
        "lines_returned: 0\n"
        "end_offset: 2\n"
        "next_offset: 2\n"
        "total_lines: 2\n"
    )


def test_read_file_caps_reads_at_1024_lines(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("\n".join(str(i) for i in range(1100)), encoding="utf-8")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 0, "line_count": 2000},
        )
    )

    header, body = out.split("\n\n", 1)
    assert header == (
        "start_offset: 0\n"
        "lines_returned: 1024\n"
        "end_offset: 1024\n"
        "next_offset: 1024\n"
        "total_lines: 1100"
    )
    assert len(body.splitlines()) == 1024
    assert body.splitlines()[-1] == "1023"


def test_read_file_rejects_binary_file(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"text\x00binary")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 0, "line_count": 1},
        )
    )

    assert out == f"error: file '{path}' appears to be binary"


def test_read_file_rejects_invalid_utf8(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"\xff\xfe\xfa")

    out = _registry().dispatch(
        ToolCall(
            id="1",
            name="read_file",
            arguments={"filename": str(path), "start_line": 0, "line_count": 1},
        )
    )

    assert out == f"error: file '{path}' appears to be binary"


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


def test_run_shell_interleaves_stdout_and_stderr():
    # Print to stdout, then stderr, then stdout again. With separate pipes the
    # returned text would be out-of-order (all stdout, then all stderr); with a
    # single merged pipe the order is chronological.
    cmd = "echo out1; echo err1 >&2; echo out2"
    out = _registry().dispatch(
        ToolCall(id="1", name="run_shell", arguments={"command": cmd})
    )
    assert out.startswith("exit code: 0")
    assert out.index("out1") < out.index("err1") < out.index("out2")


def test_run_shell_routes_through_argv_wrapper():
    seen = []

    def argv(command: str) -> list[str]:
        seen.append(command)
        return ["sh", "-c", f"echo wrapped; {command}"]

    registry = ToolRegistry(builtin_tools(shell_argv=argv))
    out = registry.dispatch(
        ToolCall(id="1", name="run_shell", arguments={"command": "echo inner"})
    )
    assert seen == ["echo inner"]
    assert out.startswith("exit code: 0")
    assert out.index("wrapped") < out.index("inner")


def test_run_shell_truncates_large_output_to_tmpfile():
    from nod.builtins import SHELL_MAX_LINES, _truncate_shell_output

    big = "\n".join(f"line {i}" for i in range(SHELL_MAX_LINES + 100))
    text, tmp_path_out = _truncate_shell_output(big)

    assert tmp_path_out is not None, "expected a tmp file path"
    assert "Full output:" in text
    assert tmp_path_out in text

    from pathlib import Path
    assert Path(tmp_path_out).read_text() == big

    lines_shown = [l for l in text.splitlines() if l.startswith("line ")]
    assert len(lines_shown) <= SHELL_MAX_LINES


def test_run_shell_no_truncation_for_small_output():
    from nod.builtins import _truncate_shell_output

    small = "line one\nline two\n"
    text, tmp = _truncate_shell_output(small)
    assert tmp is None
    assert text == small


def test_run_shell_truncation_byte_limit():
    from nod.builtins import SHELL_MAX_BYTES, _truncate_shell_output

    fat_line = "x" * (SHELL_MAX_BYTES + 1)
    big = "\n".join([fat_line] * 5)
    text, tmp = _truncate_shell_output(big)
    assert tmp is not None
    assert "Full output:" in text


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
