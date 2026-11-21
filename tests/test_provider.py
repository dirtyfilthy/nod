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


def test_payload_includes_provider_routing():
    routing = {"order": ["deepseek"], "allow_fallbacks": False}
    p = OpenAICompatibleProvider(model="m", api_key="x", provider_routing=routing)
    assert p._payload([], None)["provider"] == routing


def test_payload_omits_provider_when_routing_empty():
    assert "provider" not in _provider()._payload([], None)


PIN = {"order": ["deepseek"], "allow_fallbacks": False}


def _routed(model, **scope):
    p = OpenAICompatibleProvider(model=model, api_key="x", provider_routing=PIN | scope)
    return p._payload([], None).get("provider")


def test_routing_with_models_applies_to_those_exact_models_only():
    scope = {"models": ["deepseek/a", "deepseek/b"]}
    assert _routed("deepseek/a", **scope) == PIN
    assert _routed("deepseek/b", **scope) == PIN
    assert _routed("deepseek/a-turbo", **scope) is None
    assert _routed("openai/x", **scope) is None


def test_routing_with_model_prefix_applies_to_models_that_start_with_it():
    assert _routed("deepseek/a", model_prefix="deepseek/") == PIN
    assert _routed("openai/deepseek/a", model_prefix="deepseek/") is None


def test_routing_with_both_scopes_applies_to_a_model_matching_either():
    scope = {"models": ["openai/x"], "model_prefix": "deepseek/"}
    assert _routed("openai/x", **scope) == PIN
    assert _routed("deepseek/a", **scope) == PIN
    assert _routed("openai/y", **scope) is None


def test_routing_scope_keys_are_never_sent():
    sent = _routed("deepseek/a", models=["deepseek/a"], model_prefix="deepseek/")
    assert sent == PIN


def test_routing_takes_a_single_model_as_a_string():
    assert _routed("deepseek/a", models="deepseek/a") == PIN
    assert _routed("deepseek", models="deepseek/a") is None


def test_routing_with_empty_scope_matches_no_model():
    assert _routed("deepseek/a", models=[]) is None


REASONING_LINES = [
    'data: {"choices":[{"delta":{"reasoning":"Need ","reasoning_details":[{"type":"reasoning.text","text":"Need ","format":"unknown","index":0}]}}]}',
    'data: {"choices":[{"delta":{"reasoning":"to look.","reasoning_details":[{"type":"reasoning.text","text":"to look.","format":"unknown","index":0}]}}]}',
    'data: {"choices":[{"delta":{"content":"ok"}}]}',
    "data: [DONE]",
]


def test_consume_collects_reasoning_and_merges_details():
    msg = _provider()._consume(iter(REASONING_LINES), None, deadline=None)
    assert msg.content == "ok"
    assert msg.reasoning == "Need to look."
    assert msg.reasoning_details == [
        {"type": "reasoning.text", "text": "Need to look.", "format": "unknown", "index": 0}
    ]


def test_consume_never_streams_reasoning_to_on_token():
    tokens: list[str] = []
    _provider()._consume(iter(REASONING_LINES), tokens.append, deadline=None)
    assert tokens == ["ok"]


def test_consume_reads_reasoning_content_alias():
    lines = ['data: {"choices":[{"delta":{"reasoning_content":"hmm"}}]}', "data: [DONE]"]
    msg = _provider()._consume(iter(lines), None, deadline=None)
    assert msg.reasoning == "hmm"
    assert msg.reasoning_details == []


def test_reasoning_details_accumulator_keeps_blocks_and_appends_signature():
    from nod.provider import _ReasoningDetailsAccumulator

    acc = _ReasoningDetailsAccumulator()
    acc.add({"type": "reasoning.text", "text": "a", "index": 0})
    acc.add({"type": "reasoning.encrypted", "data": "x", "index": 1})
    acc.add({"text": "b", "signature": "s1", "index": 0})
    acc.add({"data": "y", "id": "r2", "index": 1})
    acc.add({"signature": "s2", "index": 0})

    assert acc.finish() == [
        {"type": "reasoning.text", "text": "ab", "signature": "s1s2", "index": 0},
        {"type": "reasoning.encrypted", "data": "xy", "id": "r2", "index": 1},
    ]


def test_payload_includes_reasoning_when_set():
    p = OpenAICompatibleProvider(model="m", api_key="x", reasoning={"effort": "high"})
    assert p._payload([], None)["reasoning"] == {"effort": "high"}


def test_payload_omits_reasoning_by_default():
    assert "reasoning" not in _provider()._payload([], None)


META_LINES = [
    'data: {"id":"gen-1","provider":"DeepInfra","choices":[{"delta":{"role":"assistant"}}]}',
    'data: {"id":"gen-1","provider":"DeepInfra","choices":[{"delta":{"reasoning":"hm"}}]}',
    'data: {"id":"gen-1","provider":"DeepInfra","choices":[{"delta":{"content":"ok"}}]}',
    'data: {"id":"gen-1","provider":"DeepInfra","choices":[{"delta":{}}],"usage":{"prompt_tokens":10,'
    '"completion_tokens":5,"cost":0.001,"completion_tokens_details":{"reasoning_tokens":3},'
    '"prompt_tokens_details":{"cached_tokens":4}}}',
    "data: [DONE]",
]


def test_consume_records_timing_provider_and_usage():
    # With no deadline the clock is read only for the first token and the end.
    clock = iter([1.5, 3.0])
    msg = _provider()._consume(iter(META_LINES), None, deadline=None, start=0.0, now=lambda: next(clock))
    assert msg.content == "ok"
    assert msg.meta["provider"] == "DeepInfra"
    assert msg.meta["generation_id"] == "gen-1"
    assert msg.meta["ttft_s"] == 1.5  # first chunk carrying reasoning
    assert msg.meta["duration_s"] == 3.0
    assert msg.meta["usage"] == {
        "prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 3, "cached_tokens": 4, "cost": 0.001,
    }


def test_complete_retries_retryable_failures_and_records_attempts(monkeypatch):
    from nod.messages import Message
    from nod.provider import _RetryableError

    slept: list[float] = []
    p = OpenAICompatibleProvider(model="m", api_key="x", sleep=slept.append)
    outcomes = iter([_RetryableError("stalled"), Message(role="assistant", content="ok")])

    def once(messages, tools, on_token):
        nxt = next(outcomes)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(p, "_complete_once", once)
    msg = p.complete([])
    assert msg.content == "ok"
    assert msg.meta["attempts"] == 2
    assert msg.meta["started_at"].endswith("Z")
    assert slept == [5.0]


def test_complete_gives_up_after_retries_and_skips_non_retryable(monkeypatch):
    from nod.provider import _RetryableError

    p = OpenAICompatibleProvider(model="m", api_key="x", retries=2, sleep=lambda s: None)
    calls = []

    def stalled(messages, tools, on_token):
        calls.append(1)
        raise _RetryableError("stalled")

    monkeypatch.setattr(p, "_complete_once", stalled)
    with pytest.raises(ProviderError, match="stalled"):
        p.complete([])
    assert len(calls) == 3

    calls.clear()

    def bad_request(messages, tools, on_token):
        calls.append(1)
        raise ProviderError("400 from provider: bad")

    monkeypatch.setattr(p, "_complete_once", bad_request)
    with pytest.raises(ProviderError, match="400"):
        p.complete([])
    assert len(calls) == 1


def test_complete_adds_each_call_to_the_running_usage(monkeypatch):
    p = OpenAICompatibleProvider(model="m", api_key="x", retries=1, sleep=lambda s: None)
    metas = [
        {"usage": {"prompt_tokens": 10, "completion_tokens": 2, "cost": 0.001}},
        {},  # an endpoint that reports no usage still counts as a call
        {"usage": {"prompt_tokens": 5, "completion_tokens": 1, "cost": 0.0005}},
    ]

    from nod.messages import Message

    def once(messages, tools, on_token):
        return Message(role="assistant", content="ok", meta=dict(metas.pop(0)))

    monkeypatch.setattr(p, "_complete_once", once)
    for _ in range(3):
        p.complete([])

    assert p.usage == {"calls": 3, "prompt_tokens": 15, "completion_tokens": 3, "cost": 0.0015}


def test_a_failed_call_adds_nothing_to_the_usage(monkeypatch):
    p = OpenAICompatibleProvider(model="m", api_key="x", retries=0)

    def bad_request(messages, tools, on_token):
        raise ProviderError("400 from provider")

    monkeypatch.setattr(p, "_complete_once", bad_request)
    with pytest.raises(ProviderError):
        p.complete([])

    assert p.usage["calls"] == 0
