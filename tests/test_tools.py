from __future__ import annotations

from nod.messages import ToolCall
from nod.tools import Tool, ToolRegistry, tool


def _add_tool() -> Tool:
    @tool(
        "add",
        "add two integers",
        {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    )
    def add(a: int, b: int) -> str:
        return str(a + b)

    return add


def test_schema_shape():
    registry = ToolRegistry([_add_tool()])
    schemas = registry.schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "add"
    assert "a" in schemas[0]["function"]["parameters"]["properties"]


def test_dispatch_runs_tool():
    registry = ToolRegistry([_add_tool()])
    out = registry.dispatch(ToolCall(id="1", name="add", arguments={"a": 2, "b": 3}))
    assert out == "5"


def test_dispatch_unknown_tool():
    registry = ToolRegistry()
    out = registry.dispatch(ToolCall(id="1", name="nope", arguments={}))
    assert "no such tool" in out


def test_dispatch_bad_arguments():
    registry = ToolRegistry([_add_tool()])
    out = registry.dispatch(ToolCall(id="1", name="add", arguments={"a": 1}))
    assert out.startswith("error: bad arguments")


def test_dispatch_swallows_exceptions():
    @tool("boom", "always fails")
    def boom() -> str:
        raise ValueError("kaboom")

    registry = ToolRegistry([boom])
    out = registry.dispatch(ToolCall(id="1", name="boom", arguments={}))
    assert "kaboom" in out
    assert out.startswith("error: tool 'boom' failed")
