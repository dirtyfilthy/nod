from __future__ import annotations

from nod.messages import (
    Message,
    ToolCall,
    assistant,
    system,
    tool_result,
    user,
)


def test_provider_dict_includes_tool_calls():
    msg = assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={"x": 1})])
    d = msg.to_provider_dict()
    assert d["role"] == "assistant"
    assert d["tool_calls"][0]["function"]["name"] == "echo"
    assert d["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'


def test_tool_result_dict():
    d = tool_result("c1", "result text").to_provider_dict()
    assert d["role"] == "tool"
    assert d["tool_call_id"] == "c1"
    assert d["content"] == "result text"


def test_storage_round_trip():
    original = [
        system("be nod"),
        user("hi"),
        assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={"a": "b"})]),
        tool_result("c1", "echoed"),
        assistant("done"),
    ]
    dumped = [m.to_storage_dict() for m in original]
    restored = [Message.from_storage_dict(d) for d in dumped]

    assert [m.role for m in restored] == ["system", "user", "assistant", "tool", "assistant"]
    assert restored[2].tool_calls[0].arguments == {"a": "b"}
    assert restored[3].tool_call_id == "c1"


def test_compaction_summary_serializes_as_user_for_provider():
    msg = Message(
        role="user",
        content="checkpoint",
        id="cmp1",
        kind="compaction_summary",
    )

    provider_dict = msg.to_provider_dict()
    storage_dict = msg.to_storage_dict()

    assert provider_dict["role"] == "user"
    assert provider_dict["content"] == "checkpoint"
    assert storage_dict["id"] == "cmp1"
    assert storage_dict["kind"] == "compaction_summary"


def test_from_provider_dict_handles_bad_json():
    tc = ToolCall.from_provider_dict(
        {"id": "x", "function": {"name": "f", "arguments": "{not json"}}
    )
    assert tc.arguments == {"_raw": "{not json"}


def test_assistant_sends_reasoning_details_back_verbatim():
    details = [{"type": "reasoning.text", "text": "think", "format": "unknown", "index": 0}]
    msg = Message(role="assistant", content="", reasoning="think", reasoning_details=details)

    d = msg.to_provider_dict()

    assert d["reasoning_details"] == details
    assert "reasoning_content" not in d


def test_assistant_sends_plain_reasoning_as_reasoning_content():
    d = Message(role="assistant", content="ok", reasoning="think").to_provider_dict()
    assert d["reasoning_content"] == "think"
    assert "reasoning_details" not in d


def test_non_assistant_and_reasonless_messages_send_no_reasoning():
    for msg in (user("hi"), tool_result("c1", "x"), assistant("plain")):
        d = msg.to_provider_dict()
        assert "reasoning_content" not in d and "reasoning_details" not in d


def test_storage_round_trip_keeps_reasoning():
    details = [{"type": "reasoning.text", "text": "think", "index": 0}]
    msg = Message(role="assistant", content="ok", reasoning="think", reasoning_details=details)

    restored = Message.from_storage_dict(msg.to_storage_dict())

    assert restored.reasoning == "think"
    assert restored.reasoning_details == details


def test_old_storage_without_reasoning_still_loads():
    restored = Message.from_storage_dict({"role": "assistant", "content": "hi"})
    assert restored.reasoning == ""
    assert restored.reasoning_details == []
