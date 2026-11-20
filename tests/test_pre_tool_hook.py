"""The pre-tool hook: core accepts every call; a replacement can refuse one."""

from __future__ import annotations

import sys
import types

from conftest import FakeProvider

from nod.agent import Agent, current_session
from nod.config import Config
from nod.memory import InMemoryMemory
from nod.messages import ToolCall, assistant
from nod.tools import ALWAYS_ACCEPT, Tool, ToolRegistry


def _echo_registry(ran: list[str]) -> ToolRegistry:
    def echo(text: str) -> str:
        ran.append(text)
        return f"echoed:{text}"

    return ToolRegistry([Tool("echo", "echo", {"type": "object", "properties": {}}, echo)])


def _two_calls():
    return FakeProvider(
        [
            assistant(
                tool_calls=[
                    ToolCall(id="c1", name="echo", arguments={"text": "safe"}),
                    ToolCall(id="c2", name="echo", arguments={"text": "risky"}),
                ]
            ),
            assistant("done"),
        ]
    )


def _tool_results(provider: FakeProvider) -> dict[str, str]:
    return {m.tool_call_id: m.content for m in provider.calls[1] if m.role == "tool"}


def test_default_hook_accepts_every_call():
    ran: list[str] = []
    provider = _two_calls()
    agent = Agent(provider=provider, registry=_echo_registry(ran), memory=InMemoryMemory())

    assert agent.pre_tool_hook is ALWAYS_ACCEPT
    agent.send("go")
    assert ran == ["safe", "risky"]


def test_a_refusal_replaces_the_result_and_the_tool_never_runs():
    ran: list[str] = []
    seen: list[tuple[str, str]] = []

    def gate(call: ToolCall) -> str | None:
        seen.append((call.arguments["text"], current_session()))
        return "refused: not this one" if call.arguments["text"] == "risky" else None

    provider = _two_calls()
    agent = Agent(provider=provider, registry=_echo_registry(ran), memory=InMemoryMemory(),
                  pre_tool_hook=gate)
    agent.send("go", session_id="s9")

    assert ran == ["safe"]
    assert seen == [("safe", "s9"), ("risky", "s9")]
    assert _tool_results(provider) == {"c1": "echoed:safe", "c2": "refused: not this one"}


def test_a_failing_hook_fails_closed():
    ran: list[str] = []

    def broken(call: ToolCall) -> str | None:
        raise RuntimeError("judge unreachable")

    provider = _two_calls()
    agent = Agent(provider=provider, registry=_echo_registry(ran), memory=InMemoryMemory(),
                  pre_tool_hook=broken)
    agent.send("go")

    assert ran == []
    result = _tool_results(provider)["c1"]
    assert result.startswith("error: the pre-tool check failed, so 'echo' did not run")
    assert "judge unreachable" in result


def test_the_hook_can_be_swapped_after_construction():
    ran: list[str] = []
    provider = _two_calls()
    agent = Agent(provider=provider, registry=_echo_registry(ran), memory=InMemoryMemory())
    agent.pre_tool_hook = lambda call: "refused"
    agent.send("go")

    assert ran == []
    assert set(_tool_results(provider).values()) == {"refused"}


def test_subagents_take_the_parents_hook_when_they_start(monkeypatch, tmp_path):
    # Stand in for nod-extras so build_agent wires its subagent factory, and
    # capture that factory to build a child the way AgentJobKind would.
    captured = {}
    fake = types.ModuleType("nod_extras.jobs")
    fake.SUBAGENT_SOUL = "child"
    fake.ShellJobKind = lambda: object()
    fake.AgentJobKind = lambda provider, child_agent: captured.setdefault("child_agent", child_agent)
    fake.Jobs = lambda inbox, kinds, jobs_dir: object()
    fake.job_tools = lambda jobs: []
    monkeypatch.setitem(sys.modules, "nod_extras", types.ModuleType("nod_extras"))
    monkeypatch.setitem(sys.modules, "nod_extras.jobs", fake)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    from nod.cli import build_agent

    agent = build_agent(Config(memory_dir=str(tmp_path), skills_dirs=[]))
    assert agent.pre_tool_hook is ALWAYS_ACCEPT

    def gate(call: ToolCall) -> str | None:
        return "refused"

    agent.pre_tool_hook = gate
    child = captured["child_agent"](FakeProvider([]))
    assert child.pre_tool_hook is gate
