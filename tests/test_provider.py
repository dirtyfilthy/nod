from __future__ import annotations

from nod.provider import _ToolCallAccumulator, _parse_sse_line


def test_parse_sse_line_content():
    line = 'data: {"choices":[{"delta":{"content":"hi"}}]}'
    delta = _parse_sse_line(line)
    assert delta == {"content": "hi"}


def test_parse_sse_line_done_and_blank():
    assert _parse_sse_line("data: [DONE]") is None
    assert _parse_sse_line("") is None
    assert _parse_sse_line(": keep-alive") is None


def test_tool_call_accumulator_reassembles_streamed_fragments():
    acc = _ToolCallAccumulator()
    acc.add({"index": 0, "id": "call_1", "function": {"name": "echo", "arguments": ""}})
    acc.add({"index": 0, "function": {"arguments": '{"text":'}})
    acc.add({"index": 0, "function": {"arguments": ' "hello"}'}})
    calls = acc.finish()
    assert len(calls) == 1
    assert calls[0].id == "call_1"
    assert calls[0].name == "echo"
    assert calls[0].arguments == {"text": "hello"}


def test_accumulator_handles_multiple_parallel_calls():
    acc = _ToolCallAccumulator()
    acc.add({"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}})
    acc.add({"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}})
    calls = acc.finish()
    assert [c.name for c in calls] == ["one", "two"]
