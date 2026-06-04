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


def test_from_provider_dict_handles_bad_json():
    tc = ToolCall.from_provider_dict(
        {"id": "x", "function": {"name": "f", "arguments": "{not json"}}
    )
    assert tc.arguments == {"_raw": "{not json"}
