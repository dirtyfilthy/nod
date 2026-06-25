from __future__ import annotations

from conftest import FakeProvider

from nod.agent import Agent
from nod.compaction import CompactionConfig
from nod.memory import InMemoryMemory
from nod.messages import Message, ToolCall, assistant
from nod.tools import Tool, ToolRegistry


def _registry_with_echo() -> tuple[ToolRegistry, list[dict]]:
    seen: list[dict] = []

    def echo(text: str) -> str:
        seen.append({"text": text})
        return f"echoed:{text}"

    registry = ToolRegistry(
        [
            Tool(
                name="echo",
                description="echo back the text",
                parameters={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                func=echo,
            )
        ]
    )
    return registry, seen


def test_plain_reply_no_tools():
    provider = FakeProvider([assistant("hello there")])
    agent = Agent(provider=provider, memory=InMemoryMemory(), soul="be nod")
    out = agent.send("hi", session_id="s1")
    assert out == "hello there"
    # System prompt is injected once at the start.
    assert provider.calls[0][0].role == "system"
    assert provider.calls[0][0].content == "be nod"


def test_tool_round_trip():
    registry, seen = _registry_with_echo()
    call = ToolCall(id="c1", name="echo", arguments={"text": "ping"})
    provider = FakeProvider(
        [
            assistant(tool_calls=[call]),
            assistant("done: ping"),
        ]
    )
    agent = Agent(provider=provider, registry=registry, memory=InMemoryMemory())
    out = agent.send("use echo", session_id="s2")

    assert out == "done: ping"
    assert seen == [{"text": "ping"}]
    # The second provider call must have seen the tool result fed back in.
    second_call_messages = provider.calls[1]
    tool_msgs = [m for m in second_call_messages if m.role == "tool"]
    assert tool_msgs and tool_msgs[0].content == "echoed:ping"
    assert tool_msgs[0].tool_call_id == "c1"


def test_max_turns_bound():
    # A provider that always asks for another tool call would loop forever.
    def always_tool(_messages):
        return assistant(
            tool_calls=[ToolCall(id="x", name="echo", arguments={"text": "again"})]
        )

    registry, _ = _registry_with_echo()
    provider = FakeProvider([always_tool, always_tool, always_tool, always_tool])
    agent = Agent(
        provider=provider, registry=registry, memory=InMemoryMemory(), max_turns=3
    )
    out = agent.send("loop", session_id="s3")
    assert "max_turns" in out
    assert len(provider.calls) == 3


def test_history_persists_across_sends():
    memory = InMemoryMemory()
    provider = FakeProvider([assistant("first"), assistant("second")])
    agent = Agent(provider=provider, memory=memory, soul="be nod")

    agent.send("one", session_id="s4")
    agent.send("two", session_id="s4")

    # System prompts are ephemeral and prepended fresh on each provider call.
    second_call = provider.calls[1]
    system_msgs = [m for m in second_call if m.role == "system"]
    assert len(system_msgs) == 1
    user_msgs = [m for m in second_call if m.role == "user"]
    assert [m.content for m in user_msgs] == ["one", "two"]


def test_streaming_callback_receives_tokens():
    provider = FakeProvider([assistant("streamed")])
    agent = Agent(provider=provider, memory=InMemoryMemory())
    tokens: list[str] = []
    agent.send("hi", session_id="s5", on_token=tokens.append)
    assert tokens == ["streamed"]


def test_start_of_turn_compaction_is_persisted():
    memory = InMemoryMemory()
    old = Message(role="user", content="x" * 200)
    recent = Message(role="user", content="recent")
    memory.persist_messages("s6", [old, recent])
    provider = FakeProvider([assistant("summary"), assistant("answer")])
    agent = Agent(
        provider=provider,
        memory=memory,
        compaction=CompactionConfig(
            context_window=20,
            reserve_tokens=0,
            keep_recent_tokens=1,
        ),
    )

    out = agent.send("next", session_id="s6")
    loaded = memory.load("s6")

    assert out == "answer"
    assert loaded[0].kind == "compaction_summary"
    assert loaded[0].content == "summary"
    assert [m.content for m in loaded[1:]] == ["recent", "next", "answer"]
    normal_call = provider.calls[1]
    assert normal_call[0].kind == "compaction_summary"
    assert [m.content for m in normal_call if m.role == "user"] == [
        "summary",
        "recent",
        "next",
    ]
