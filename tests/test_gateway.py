from __future__ import annotations

import io
import threading
import time

from conftest import FakeProvider

from nod.agent import Agent
from nod.gateway import CLIGateway
from nod.memory import InMemoryMemory
from nod.messages import assistant


def _run_cli(monkeypatch, agent, lines, during=None):
    """Drive the CLI with scripted input; `during(i)` runs before line i is read."""

    out = io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    it = iter(enumerate(lines))

    def fake_input(_prompt=""):
        try:
            i, line = next(it)
        except StopIteration:
            raise EOFError
        if during is not None:
            during(i)
        return line

    monkeypatch.setattr("builtins.input", fake_input)
    gateway = CLIGateway(session_id="cli")
    gateway.run(agent)
    return out.getvalue()


def test_cli_sends_lines_and_prints_replies(monkeypatch):
    agent = Agent(provider=FakeProvider([assistant("one"), assistant("two")]), memory=InMemoryMemory())
    out = _run_cli(monkeypatch, agent, ["hi", "", "again", "exit"])
    assert "nod> one" in out
    assert "nod> two" in out
    assert len(agent.provider.calls) == 2


def test_cli_wakes_on_inbox_post_while_waiting_for_input(monkeypatch):
    agent = Agent(provider=FakeProvider([assistant("heard it"), assistant("bye")]), memory=InMemoryMemory())
    woke = threading.Event()

    def during(i):
        if i == 1:
            # The user is "typing" the second line; a job finishes meanwhile.
            agent.inbox.post("cli", "<job-finished id=1>done</job-finished>")
            deadline = time.time() + 5
            while time.time() < deadline and not agent.provider.calls:
                time.sleep(0.01)
            woke.set()

    out = _run_cli(monkeypatch, agent, ["", "last"], during)
    assert woke.is_set()
    assert "nod> heard it" in out
    assert "nod> bye" in out
    first_call_users = [m for m in agent.provider.calls[0] if m.role == "user"]
    assert [m.kind for m in first_call_users] == ["event"]


def test_cli_delivers_events_left_from_before_startup(monkeypatch):
    agent = Agent(provider=FakeProvider([assistant("lost job noted")]), memory=InMemoryMemory())
    agent.inbox.post("cli", "<job-finished id=3 status=lost>x</job-finished>")
    out = _run_cli(monkeypatch, agent, ["exit"])
    assert "lost job noted" in out
