from __future__ import annotations

from nod.builtins import builtin_tools
from nod.messages import ToolCall
from nod.tools import ToolRegistry


def _registry() -> ToolRegistry:
    return ToolRegistry(builtin_tools())


def test_builtin_schemas_are_functions():
    schemas = _registry().schemas()
    names = {schema["function"]["name"] for schema in schemas}
    assert names == {"read_file", "write_file", "list_dir", "run_shell", "http_get"}
    assert all(schema["type"] == "function" for schema in schemas)


def test_read_write_and_list_dir(tmp_path):
    registry = _registry()
    path = tmp_path / "nested" / "note.txt"

    written = registry.dispatch(
        ToolCall(
            id="1",
            name="write_file",
            arguments={"path": str(path), "content": "hello"},
        )
    )
    assert written.startswith("wrote 5 bytes")

    read = registry.dispatch(
        ToolCall(id="2", name="read_file", arguments={"path": str(path)})
    )
    assert read == "hello"

    listed = registry.dispatch(
        ToolCall(id="3", name="list_dir", arguments={"path": str(tmp_path)})
    )
    assert listed == "nested/"


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
