from __future__ import annotations

from conftest import FakeProvider

from nod.compaction import (
    SUMMARY_TOOL_RESULT_LIMIT,
    CompactionConfig,
    compact_if_needed,
    estimate_tokens,
    find_cut_point,
    serialize_for_summary,
)
from nod.messages import Message, ToolCall, assistant, tool_result, user


def test_estimate_tokens_counts_content_and_tool_calls():
    msg = assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": "y"})])
    assert estimate_tokens(user("abcd")) == 1
    assert estimate_tokens(msg) > 1


def test_find_cut_point_never_cuts_at_tool_result():
    messages = [
        user("old"),
        assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={})]),
        tool_result("c1", "x" * 1000),
    ]

    assert find_cut_point(messages, keep_recent_tokens=1) == 1


def test_serialize_for_summary_truncates_tool_results():
    content = "x" * (SUMMARY_TOOL_RESULT_LIMIT + 10)
    text = serialize_for_summary([tool_result("c1", content)])

    assert "[truncated 10 characters]" in text
    assert len(text) < len(content) + 100


def test_compact_if_needed_noops_under_budget():
    messages = [user("short")]
    provider = FakeProvider([])
    cfg = CompactionConfig(context_window=100, reserve_tokens=10)

    result = compact_if_needed(messages, provider, cfg)

    assert result.messages == messages
    assert not result.compacted
    assert provider.calls == []


def test_compact_if_needed_summarizes_over_budget():
    old = user("x" * 200)
    old.id = "old"
    recent = user("recent")
    recent.id = "recent"
    provider = FakeProvider([Message(role="assistant", content="summary")])
    cfg = CompactionConfig(
        context_window=20,
        reserve_tokens=0,
        keep_recent_tokens=1,
    )

    result = compact_if_needed([old, recent], provider, cfg)

    assert result.compacted
    assert result.summary == "summary"
    assert result.first_kept_entry_id == "recent"
    assert result.messages[0].kind == "compaction_summary"
    assert result.messages[1] is recent
    assert provider.calls[0][0].role == "system"


def test_estimate_tokens_counts_reasoning_sent_back():
    plain = assistant("ok")
    thinking = Message(role="assistant", content="ok", reasoning="x" * 400)
    assert estimate_tokens(thinking) >= estimate_tokens(plain) + 100
