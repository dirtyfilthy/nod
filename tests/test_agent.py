from __future__ import annotations

import pytest
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


def test_reasoning_is_sent_back_on_later_steps_and_stored():
    registry, _ = _registry_with_echo()
    details = [{"type": "reasoning.text", "text": "use echo", "format": "unknown", "index": 0}]
    first = Message(
        role="assistant",
        reasoning="use echo",
        reasoning_details=details,
        tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hi"})],
    )
    provider = FakeProvider([first, assistant("done")])
    memory = InMemoryMemory()
    agent = Agent(provider=provider, registry=registry, memory=memory)

    agent.send("go", session_id="s1")

    # The second request carries the first step's reasoning, as DeepSeek's
    # thinking mode requires within a tool loop.
    sent = [m.to_provider_dict() for m in provider.calls[1] if m.role == "assistant"]
    assert sent[0]["reasoning_details"] == details
    stored = [m for m in memory.load("s1") if m.role == "assistant"]
    assert stored[0].reasoning == "use echo"


# --- inbox events and wake-ups ---------------------------------------------


def _event_messages(messages):
    return [m for m in messages if m.kind == "event"]


def test_event_posted_mid_turn_is_delivered_before_next_provider_call():
    registry, _ = _registry_with_echo()
    memory = InMemoryMemory()
    agent = Agent(provider=FakeProvider([]), registry=registry, memory=memory)

    def ask_echo(_messages):
        # Something off the loop posts while the model is "thinking".
        agent.inbox.post("s", "<job-finished id=1>done</job-finished>")
        return assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})])

    def check(messages):
        events = _event_messages(messages)
        assert [e.content for e in events] == ["<job-finished id=1>done</job-finished>"]
        assert events[0].role == "user"
        # The event is appended after the tool result, right before this call.
        assert messages[-1].kind == "event"
        assert messages[-2].role == "tool"
        return assistant("ok")

    agent.provider = FakeProvider([ask_echo, check])
    assert agent.send("go", session_id="s") == "ok"
    assert agent.inbox.pending("s") == []
    stored = memory.load("s")
    assert [m.content for m in _event_messages(stored)] == ["<job-finished id=1>done</job-finished>"]


def test_pending_event_precedes_new_user_text():
    agent = Agent(provider=FakeProvider([assistant("hi")]), memory=InMemoryMemory())
    agent.inbox.post("s", "event while idle")

    agent.send("hello", session_id="s")

    users = [m for m in agent.provider.calls[0] if m.role == "user"]
    assert [(m.kind, m.content) for m in users] == [
        ("event", "event while idle"),
        ("message", "hello"),
    ]


def test_event_survives_failing_provider_call():
    def boom(_messages):
        raise RuntimeError("brain offline")

    memory = InMemoryMemory()
    agent = Agent(provider=FakeProvider([boom]), memory=memory)
    agent.inbox.post("s", "ev")

    with pytest.raises(RuntimeError):
        agent.send("hello", session_id="s")

    # Persisted before the call, acknowledged after the persist: the event is
    # in the transcript, not lost. So is the user's text.
    stored = memory.load("s")
    assert [(m.kind, m.content) for m in stored] == [("event", "ev"), ("message", "hello")]
    assert agent.inbox.pending("s") == []


def test_wake_runs_a_turn_only_when_something_is_pending():
    provider = FakeProvider([assistant("noted")])
    memory = InMemoryMemory()
    agent = Agent(provider=provider, memory=memory, soul="be nod")

    assert agent.wake("s") is None
    assert provider.calls == []

    agent.inbox.post("s", "<job-finished id=2>x</job-finished>")
    assert agent.wake("s") == "noted"
    users = [m for m in provider.calls[0] if m.role == "user"]
    assert [m.kind for m in users] == ["event"]
    assert [m.content for m in memory.load("s")] == [
        "<job-finished id=2>x</job-finished>",
        "noted",
    ]
    # Delivered once: a second wake has nothing to do.
    assert agent.wake("s") is None


def test_wake_after_mid_turn_delivery_does_nothing():
    registry, _ = _registry_with_echo()
    agent = Agent(provider=FakeProvider([]), registry=registry, memory=InMemoryMemory())

    def ask_echo(_messages):
        agent.inbox.post("s", "ev")
        return assistant(tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "x"})])

    agent.provider = FakeProvider([ask_echo, assistant("done")])
    agent.send("go", session_id="s")
    calls_before = len(agent.provider.calls)
    assert agent.wake("s") is None
    assert len(agent.provider.calls) == calls_before


def test_consecutive_wakeups_are_capped_and_send_resets():
    # Every wake-up posts a new event, like a job that starts another job.
    agent = Agent(provider=FakeProvider([]), memory=InMemoryMemory(), max_wakeups=2)

    def renew(_messages):
        agent.inbox.post("s", "again")
        return assistant("ok")

    agent.provider = FakeProvider([renew] * 10)
    agent.inbox.post("s", "first")
    assert agent.wake("s") == "ok"
    assert agent.wake("s") == "ok"
    assert agent.wake("s") is None  # cap hit; the event stays in the inbox
    assert [e.text for e in agent.inbox.pending("s")] == ["again"]
    assert len(agent.provider.calls) == 2

    # A user turn delivers what was waiting and resets the count.
    agent.send("hello", session_id="s")
    assert agent.wake("s") == "ok"


def test_send_and_wake_share_a_session_lock():
    import threading

    agent = Agent(provider=FakeProvider([assistant("a"), assistant("b")]), memory=InMemoryMemory())
    agent.inbox.post("s", "ev")
    with agent.session_lock("s"):
        t = threading.Thread(target=agent.wake, args=("s",))
        t.start()
        t.join(timeout=0.2)
        assert t.is_alive()  # blocked on the lock we hold
    t.join(timeout=2)
    assert not t.is_alive()
