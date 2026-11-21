from __future__ import annotations

import io
import json
import sys

from conftest import FakeProvider

from nod import cli
from nod.agent import Agent
from nod.memory import InMemoryMemory
from nod.messages import assistant


def _wire(monkeypatch, tmp_path, replies):
    """Run `main` in an empty dir with a scripted agent in place of the real one."""

    monkeypatch.chdir(tmp_path)
    # main() turns SIGTERM into an exit; keep that out of the test process.
    monkeypatch.setattr(cli.signal, "signal", lambda *a: None)
    agent = Agent(provider=FakeProvider(replies), memory=InMemoryMemory())
    monkeypatch.setattr(cli, "build_agent", lambda cfg: agent)
    return agent


def _user_texts(agent, call=0):
    return [m.content for m in agent.provider.calls[call] if m.role == "user"]


def test_run_takes_instructions_from_the_command_line(monkeypatch, tmp_path, capsys):
    agent = _wire(monkeypatch, tmp_path, [assistant("done it")])

    assert cli.main(["run", "fix", "the", "bug"]) == 0

    out, err = capsys.readouterr()
    assert out == "done it\n"
    assert _user_texts(agent) == ["fix the bug"]
    assert "[session run-" in err


def test_run_reads_a_file_and_puts_extra_text_after_it(monkeypatch, tmp_path, capsys):
    agent = _wire(monkeypatch, tmp_path, [assistant("ok")])
    (tmp_path / "task.md").write_text("Step one.\nStep two.\n")

    assert cli.main(["run", "-f", "task.md", "--session", "s1", "and be quick"]) == 0

    assert _user_texts(agent) == ["Step one.\nStep two.\n\nand be quick"]
    assert agent.memory.load("s1")[-1].content == "ok"
    assert "[session s1]" in capsys.readouterr().err


def test_run_reads_stdin_for_a_dash(monkeypatch, tmp_path):
    agent = _wire(monkeypatch, tmp_path, [assistant("ok")])
    monkeypatch.setattr(sys, "stdin", io.StringIO("from a pipe\n"))

    assert cli.main(["run", "-f", "-"]) == 0
    assert _user_texts(agent) == ["from a pipe"]


def test_run_without_instructions_or_with_a_missing_file_exits_2(monkeypatch, tmp_path, capsys):
    agent = _wire(monkeypatch, tmp_path, [])

    assert cli.main(["run"]) == 2
    assert "no instructions" in capsys.readouterr().err
    assert cli.main(["run", "-f", "nope.md"]) == 2
    assert "nope.md" in capsys.readouterr().err
    assert agent.provider.calls == []


def test_run_gives_a_fresh_session_each_time(monkeypatch, tmp_path, capsys):
    agent = _wire(monkeypatch, tmp_path, [assistant("a"), assistant("b")])

    cli.main(["run", "one"])
    cli.main(["run", "two"])

    # The second run saw none of the first one's history.
    assert _user_texts(agent, 1) == ["two"]
    sessions = [line for line in capsys.readouterr().err.splitlines() if line.startswith("[session")]
    assert len(set(sessions)) == 2


def test_run_wakes_for_an_event_that_missed_the_last_call(monkeypatch, tmp_path, capsys):
    def finish_job_meanwhile(messages):
        agent.inbox.post("s", "<job-finished id=1>done</job-finished>")
        return assistant("started a job")

    agent = _wire(monkeypatch, tmp_path, [finish_job_meanwhile, assistant("job's done")])

    assert cli.main(["run", "--session", "s", "go"]) == 0
    assert capsys.readouterr().out == "job's done\n"
    assert [m.kind for m in agent.provider.calls[1] if m.role == "user"][-1] == "event"


def test_run_reports_a_failing_provider_and_exits_1(monkeypatch, tmp_path, capsys):
    def boom(messages):
        raise RuntimeError("provider down")

    _wire(monkeypatch, tmp_path, [boom])

    assert cli.main(["run", "--session", "s", "go"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "[error] provider down" in err and "[session s]" in err


def test_model_flag_overrides_the_config(monkeypatch, tmp_path):
    seen = []
    _wire(monkeypatch, tmp_path, [assistant("a"), assistant("b")])
    agent_for = cli.build_agent
    monkeypatch.setattr(cli, "build_agent", lambda cfg: seen.append(cfg.model) or agent_for(cfg))
    (tmp_path / "config.toml").write_text('model = "from/config"\n')

    cli.main(["run", "go"])
    cli.main(["--model", "from/flag", "run", "go"])

    assert seen == ["from/config", "from/flag"]


def test_max_turns_flag_overrides_the_config(monkeypatch, tmp_path):
    seen = []
    _wire(monkeypatch, tmp_path, [assistant("a"), assistant("b")])
    agent_for = cli.build_agent
    monkeypatch.setattr(cli, "build_agent", lambda cfg: seen.append(cfg.max_turns) or agent_for(cfg))
    (tmp_path / "config.toml").write_text("max_turns = 7\n")

    cli.main(["run", "go"])
    cli.main(["--max-turns", "3", "run", "go"])

    assert seen == [7, 3]


def test_sigterm_handler_is_in_place_before_the_agent_is_built(monkeypatch, tmp_path):
    # Building the agent starts the sandbox container, so a SIGTERM during the
    # build has to reach the handler or the container is left behind.
    order = []
    agent = _wire(monkeypatch, tmp_path, [assistant("ok")])
    monkeypatch.setattr(cli.signal, "signal", lambda *a: order.append("handler"))
    monkeypatch.setattr(cli, "build_agent", lambda cfg: order.append("build") or agent)

    cli.main(["run", "go"])

    assert order == ["handler", "build"]


def test_run_prints_what_the_provider_used(monkeypatch, tmp_path, capsys):
    agent = _wire(monkeypatch, tmp_path, [assistant("ok")])
    agent.provider.usage = {"calls": 2, "prompt_tokens": 30, "completion_tokens": 5, "cost": 0.0012}

    assert cli.main(["run", "--session", "s", "go"]) == 0

    err = capsys.readouterr().err.splitlines()
    assert err[-1] == "[session s]"
    assert err[-2].startswith("[usage ") and err[-2].endswith("]")
    assert json.loads(err[-2][len("[usage ") : -1]) == agent.provider.usage


def test_run_prints_usage_when_the_turn_fails_too(monkeypatch, tmp_path, capsys):
    def boom(messages):
        raise RuntimeError("provider down")

    agent = _wire(monkeypatch, tmp_path, [boom])
    agent.provider.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0}

    assert cli.main(["run", "go"]) == 1
    assert '[usage {"calls": 0' in capsys.readouterr().err


def test_run_prints_no_usage_line_for_a_provider_that_keeps_none(monkeypatch, tmp_path, capsys):
    _wire(monkeypatch, tmp_path, [assistant("ok")])

    cli.main(["run", "go"])

    assert "[usage" not in capsys.readouterr().err
