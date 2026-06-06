from __future__ import annotations

import pytest

from nod.provider import (
    OpenAICompatibleProvider,
    ProviderError,
    _ToolCallAccumulator,
    _parse_sse_line,
)

CONTENT_LINES = [
    'data: {"choices":[{"delta":{"content":"hel"}}]}',
    'data: {"choices":[{"delta":{"content":"lo"}}]}',
    "data: [DONE]",
]


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


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(model="m", api_key="x")


def test_consume_assembles_stream_without_deadline():
    msg = _provider()._consume(iter(CONTENT_LINES), None, deadline=None)
    assert msg.content == "hello"


def test_consume_streams_tokens_to_callback():
    tokens: list[str] = []
    _provider()._consume(iter(CONTENT_LINES), tokens.append, deadline=None)
    assert tokens == ["hel", "lo"]


def test_consume_aborts_when_deadline_passed():
    # A trickle of keep-alive comments that never completes: with a clock already
    # past the deadline, the very first line trips the wall-clock guard.
    trickle = iter([": keep-alive"] * 5)
    with pytest.raises(ProviderError, match="deadline"):
        _provider()._consume(trickle, None, deadline=10.0, now=lambda: 11.0)


def test_consume_within_deadline_succeeds():
    msg = _provider()._consume(
        iter(CONTENT_LINES), None, deadline=100.0, now=lambda: 1.0
    )
    assert msg.content == "hello"
