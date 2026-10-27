import importlib.util
import os
import time
from pathlib import Path


def _load_run_nod_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "bench" / "swebench" / "run_nod.py"
    spec = importlib.util.spec_from_file_location("swebench_run_nod", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bench_agent_loads_system_skills(monkeypatch):
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    agent = run_nod.build_bench_agent(
        model="test-model",
        base_url="https://example.invalid/v1",
        max_turns=7,
        traces_dir=None,
    )

    prompt = agent.system_prompt()
    assert "When a task matches a skill, read it before guessing." in prompt
    assert "Available skills" in prompt
    assert "plan: Make a concise implementation plan" in prompt
    assert "read_skill" in agent.registry


def _chdir_and_report(inst):
    # Module-level so the spawned worker processes can unpickle it.
    os.chdir(inst["dir"])
    # Barrier: wait (bounded) until every job has started. Sequential execution
    # can never get past it, so `all_started` proves the jobs ran at once without
    # depending on timing.
    Path(inst["barrier"], inst["instance_id"]).touch()
    deadline = time.monotonic() + 30
    while len(os.listdir(inst["barrier"])) < inst["n"] and time.monotonic() < deadline:
        time.sleep(0.01)
    all_started = len(os.listdir(inst["barrier"])) == inst["n"]
    return {"instance_id": inst["instance_id"], "model_patch": os.getcwd(), "all_started": all_started}


def test_solve_all_parallel_workers_keep_their_own_cwd(tmp_path):
    run_nod = _load_run_nod_module()
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    rows = []
    for i in range(4):
        d = tmp_path / f"inst{i}"
        d.mkdir()
        rows.append({"instance_id": f"inst{i}", "dir": str(d), "barrier": str(barrier), "n": 4})

    cwd = os.getcwd()
    results = dict(
        (inst["instance_id"], pred)
        for inst, pred in run_nod.solve_all(rows, _chdir_and_report, workers=4)
    )

    assert os.getcwd() == cwd
    assert set(results) == {r["instance_id"] for r in rows}
    for row in rows:
        assert os.path.realpath(results[row["instance_id"]]["model_patch"]) == os.path.realpath(row["dir"])
    assert all(pred["all_started"] for pred in results.values())


def test_solve_all_single_worker_runs_inline_in_order():
    run_nod = _load_run_nod_module()
    rows = [{"instance_id": f"inst{i}"} for i in range(3)]

    out = list(run_nod.solve_all(rows, lambda inst: {"instance_id": inst["instance_id"]}, workers=1))

    assert [inst["instance_id"] for inst, _ in out] == ["inst0", "inst1", "inst2"]


def test_bench_agent_pins_one_provider_without_fallbacks(monkeypatch):
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    common = dict(model="test-model", base_url="https://example.invalid/v1", max_turns=7, traces_dir=None)

    pinned = run_nod.build_bench_agent(**common)
    assert pinned.provider.provider_routing == {"order": [run_nod.DEFAULT_PROVIDER], "allow_fallbacks": False}

    unpinned = run_nod.build_bench_agent(**common, provider="")
    assert unpinned.provider.provider_routing == {}


def test_bench_agent_has_no_network_tool(monkeypatch):
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    agent = run_nod.build_bench_agent(
        model="test-model", base_url="https://example.invalid/v1", max_turns=7, traces_dir=None
    )

    assert "http_get" not in agent.registry
    assert "run_shell" in agent.registry
    assert "no internet access" in agent.system_prompt()


def test_bench_agent_sets_reasoning_effort(monkeypatch):
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    common = dict(model="test-model", base_url="https://example.invalid/v1", max_turns=7, traces_dir=None)

    assert run_nod.build_bench_agent(**common).provider.reasoning == {"effort": run_nod.DEFAULT_REASONING_EFFORT}
    assert run_nod.build_bench_agent(**common, reasoning_effort="").provider.reasoning == {}
    assert run_nod.build_bench_agent(**common, reasoning_effort="default").provider.reasoning == {}


def test_workspace_names_do_not_reveal_the_instance(monkeypatch, tmp_path):
    run_nod = _load_run_nod_module()
    seen = {}

    def fake_start(image, name, host_dir, scratch_dir, log=print):
        seen.update(name=name, host_dir=host_dir, scratch_dir=scratch_dir)
        return False  # stop before any docker work

    monkeypatch.setattr(run_nod, "start_container", fake_start)
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: None)
    inst = {"instance_id": "psf__requests-863", "image": "img", "base_commit": "x", "problem_statement": "p"}

    run_nod.solve_instance(inst, model="m", base_url="u", max_turns=1, workdir=str(tmp_path), traces_dir=None)

    for value in (seen["name"], os.path.relpath(seen["host_dir"], tmp_path), os.path.relpath(seen["scratch_dir"], tmp_path)):
        assert "863" not in value and "requests" not in value


def test_scratch_dir_is_outside_the_repo_and_cleaned_up(monkeypatch, tmp_path):
    run_nod = _load_run_nod_module()
    seen = {}

    def fake_start(image, name, host_dir, scratch_dir, log=print):
        # It must exist before docker run, or docker creates it as root.
        seen.update(host_dir=host_dir, scratch_dir=scratch_dir, existed=os.path.isdir(scratch_dir))
        return False

    monkeypatch.setattr(run_nod, "start_container", fake_start)
    removed = []
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: removed.append(cmd))
    inst = {"instance_id": "i", "image": "img", "base_commit": "x", "problem_statement": "p"}

    run_nod.solve_instance(inst, model="m", base_url="u", max_turns=1, workdir=str(tmp_path), traces_dir=None)

    assert seen["existed"]
    assert not seen["scratch_dir"].startswith(seen["host_dir"] + os.sep)
    assert not os.path.exists(seen["scratch_dir"])
    assert list(tmp_path.iterdir()) == []
    assert removed and removed[0][:3] == ["docker", "rm", "-f"]


def test_start_container_mounts_scratch_dir_at_its_own_path(monkeypatch):
    import subprocess

    run_nod = _load_run_nod_module()
    cmds = []

    def fake_run(cmd, **kw):
        cmds.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(run_nod, "_run", fake_run)

    assert run_nod.start_container("img", "c", "/w/repo", "/w/repo-tmp", log=lambda m: None)

    docker_run = next(c for c in cmds if c[:2] == ["docker", "run"])
    assert "/w/repo-tmp:/w/repo-tmp" in docker_run


def test_container_shell_argv_sets_tmpdir_and_bench_timeout():
    run_nod = _load_run_nod_module()

    argv = run_nod.container_shell_argv("c", "/w/repo-tmp")("echo hi")

    assert "TMPDIR=/w/repo-tmp" in argv
    i = argv.index("timeout")
    assert argv[i:i + 4] == ["timeout", "-k", "5", str(run_nod.SHELL_TIMEOUT_SECONDS)]
    assert run_nod.SHELL_TIMEOUT_SECONDS == 180
    assert argv[-1] == "echo hi"


def test_bench_agent_uses_bench_timeout_and_scratch_dir(monkeypatch, tmp_path):
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    calls = []

    def argv(command):
        calls.append(command)
        return ["sh", "-c", command]

    agent = run_nod.build_bench_agent(
        model="test-model", base_url="https://example.invalid/v1", max_turns=7,
        traces_dir=None, shell_argv=argv, scratch_dir=str(tmp_path),
    )

    prompt = agent.system_prompt()
    assert f"$TMPDIR, which is {tmp_path}" in prompt
    assert "time out after 180 seconds" in prompt

    from nod.builtins import SHELL_MAX_LINES
    from nod.messages import ToolCall

    out = agent.registry.dispatch(
        ToolCall(id="1", name="run_shell", arguments={"command": f"seq 1 {SHELL_MAX_LINES + 10}"})
    )
    assert calls
    assert f"Full output: {tmp_path}" in out


def test_pull_image_waits_out_docker_hub_rate_limit(monkeypatch):
    import subprocess

    run_nod = _load_run_nod_module()
    # The wordings seen on the box; none says "toomanyrequests".
    results = iter([
        subprocess.CompletedProcess([], 1, "", "error from registry: You have reached your unauthenticated pull rate limit."),
        subprocess.CompletedProcess([], 1, "", 'Error response from daemon: unknown: failed to resolve reference "docker.io/x:latest": unexpected status from HEAD request to https://registry-1.docker.io/v2/x/manifests/latest: 429 Too Many Requests'),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: next(results))
    slept = []

    assert run_nod.pull_image("img", log=lambda m: None, sleep=slept.append)
    assert slept == [run_nod.PULL_RATE_LIMIT_WAIT_SECONDS] * 2


def test_pull_image_gives_up_on_other_errors_and_after_max_wait(monkeypatch):
    import subprocess

    run_nod = _load_run_nod_module()
    slept = []
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "Error response from daemon: manifest unknown"))
    assert not run_nod.pull_image("img", log=lambda m: None, sleep=slept.append)
    assert slept == []  # permanent errors fail at once

    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "toomanyrequests"))
    slept = []
    assert not run_nod.pull_image("img", log=lambda m: None, sleep=slept.append)
    assert sum(slept) == run_nod.PULL_RATE_LIMIT_MAX_WAIT_SECONDS


# --- --extras: nod-extras' background jobs in the bench ---------------------------


def test_container_job_argv_has_no_bench_timeout_and_records_its_group(tmp_path):
    run_nod = _load_run_nod_module()

    argv = run_nod.container_job_argv("c", "/w/repo-tmp", str(tmp_path))("pytest -x")

    assert "TMPDIR=/w/repo-tmp" in argv
    assert "timeout" not in argv
    i = argv.index("c")
    assert argv[i + 1:i + 3] == ["setsid", "-w"]
    assert argv[-2] == "pytest -x"
    assert os.path.dirname(argv[-1]) == str(tmp_path)  # where the group id goes
    assert argv[-1] != run_nod.container_job_argv("c", "/w", str(tmp_path))("x")[-1]


def test_container_job_kill_signals_the_group_inside_the_container(monkeypatch, tmp_path):
    import signal
    import subprocess

    run_nod = _load_run_nod_module()
    docker, host = [], []
    status = {"code": 0}
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: (docker.append(cmd), subprocess.CompletedProcess(cmd, status["code"]))[1])
    monkeypatch.setattr(run_nod.os, "killpg", lambda pid, sig: host.append((pid, sig)))

    class Proc:
        pid = 4242
        args = ["docker", "exec", "c", "bash", "cmd", str(tmp_path / "pg")]

    kill = run_nod.container_job_kill("c")
    (tmp_path / "pg").write_text("17\n")

    kill(Proc, signal.SIGTERM)
    # dash's kill (the images' sh) rejects `--`.
    assert docker == [["docker", "exec", "-u", "0", "c", "sh", "-c", f"kill -{int(signal.SIGTERM)} -17"]]
    assert host == []  # docker exec stays up until the command really ends

    kill(Proc, signal.SIGKILL)
    assert host == [(4242, signal.SIGKILL)]

    host.clear()
    status["code"] = 1  # the container couldn't be reached
    kill(Proc, signal.SIGTERM)
    assert host == [(4242, signal.SIGTERM)]

    host.clear(), docker.clear()
    (tmp_path / "pg").unlink()  # the command hasn't written its group yet
    kill(Proc, signal.SIGTERM)
    assert docker == [] and host == [(4242, signal.SIGTERM)]


def _jobs_agent(run_nod, tmp_path, **kw):
    return run_nod.build_bench_agent_with_jobs(
        model="test-model", base_url="https://example.invalid/v1", max_turns=7, traces_dir=None,
        shell_argv=None, scratch_dir=str(tmp_path), jobs_dir=str(tmp_path / "jobs"), container="c", **kw,
    )


def test_bench_agent_with_jobs_adds_the_job_tools_and_says_so(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("nod_extras.jobs")
    run_nod = _load_run_nod_module()
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")

    plain = run_nod.build_bench_agent(model="m", base_url="https://example.invalid/v1", max_turns=7, traces_dir=None)
    agent, jobs = _jobs_agent(run_nod, tmp_path)
    try:
        assert "spawn_agent" not in plain.registry and "Background jobs:" not in plain.system_prompt()
        for name in ("start_shell_job", "spawn_agent", "list_jobs", "kill_job", "wait_jobs", "run_shell"):
            assert name in agent.registry
        assert "http_get" not in agent.registry
        prompt = agent.system_prompt()
        assert "Background jobs:" in prompt and "without its\n  180-second limit" in prompt
        assert agent.inbox is jobs.inbox
        assert (tmp_path / "jobs" / "pids").is_dir()

        # A subagent gets the bench tools and environment, never the job tools.
        child = jobs.kinds["agent"].child_factory(agent.provider)
        assert "spawn_agent" not in child.registry and "start_shell_job" not in child.registry
        assert "run_shell" in child.registry and "http_get" not in child.registry
        assert child.soul.startswith("You are a subagent")
        assert "Don't edit the\nrepository's test files" in child.soul
        assert f"$TMPDIR, which is {tmp_path}" in child.soul
    finally:
        jobs.shutdown()


def test_settle_jobs_wakes_the_agent_for_a_job_that_ends_after_its_reply(monkeypatch, tmp_path):
    import pytest

    pytest.importorskip("nod_extras.jobs")
    from conftest import FakeProvider
    from nod.agent import Agent
    from nod.inbox import InMemoryInbox
    from nod.memory import InMemoryMemory
    from nod.messages import Message
    from nod.tools import ToolRegistry
    from nod_extras.jobs import Jobs, ShellJobKind

    run_nod = _load_run_nod_module()
    inbox = InMemoryInbox()
    jobs = Jobs(inbox, [ShellJobKind()], jobs_dir=tmp_path, poll_interval=0.02)
    provider = FakeProvider([Message(role="assistant", content="tests passed, done")])
    agent = Agent(provider=provider, registry=ToolRegistry([]), memory=InMemoryMemory(), inbox=inbox)
    try:
        jobs.start("shell", "s", description="command: tests", command="sleep 0.3; echo ok")
        run_nod.settle_jobs(agent, jobs, "s", log=lambda m: None)

        assert len(provider.calls) == 1
        assert "<job-finished id=1 kind=shell status=done" in provider.calls[0][-1].content
        assert inbox.pending("s") == []
        assert [j.status for j in jobs.list("s")] == ["done"]
    finally:
        jobs.shutdown()


def test_settle_jobs_stops_at_the_wake_up_cap(tmp_path):
    import pytest

    pytest.importorskip("nod_extras.jobs")
    from conftest import FakeProvider
    from nod.agent import Agent
    from nod.inbox import InMemoryInbox
    from nod.memory import InMemoryMemory
    from nod.tools import ToolRegistry
    from nod_extras.jobs import Jobs, ShellJobKind

    run_nod = _load_run_nod_module()
    inbox = InMemoryInbox()
    jobs = Jobs(inbox, [ShellJobKind()], jobs_dir=tmp_path, poll_interval=0.02)
    agent = Agent(provider=FakeProvider([]), registry=ToolRegistry([]), memory=InMemoryMemory(), inbox=inbox, max_wakeups=0)
    logged = []
    try:
        jobs.start("shell", "s", description="command: true", command="true")
        run_nod.settle_jobs(agent, jobs, "s", log=logged.append)
        assert logged[-1] == "! wake-up cap hit with job events unread"
        assert len(inbox.pending("s")) == 1
    finally:
        jobs.shutdown()


def test_extras_runs_under_a_neutral_session_and_renames_its_traces(monkeypatch, tmp_path):
    import subprocess

    run_nod = _load_run_nod_module()
    seen = {}
    traces = tmp_path / "traces"
    traces.mkdir()
    work = tmp_path / "work"

    class FakeJobs:
        def shutdown(self):
            seen["shutdown"] = True

    class FakeAgent:
        def send(self, prompt, session_id):
            seen["session"] = session_id
            (traces / f"{session_id}.jsonl").write_text("{}\n")
            (traces / f"{session_id}_job-1.jsonl").write_text("{}\n")

    def fake_build(**kw):
        seen.update(jobs_dir=kw["jobs_dir"], scratch_dir=kw["scratch_dir"], container=kw["container"])
        return FakeAgent(), FakeJobs()

    def fake_start(image, name, host_dir, scratch_dir, log=print):
        os.makedirs(host_dir)
        return True

    monkeypatch.setattr(run_nod, "start_container", fake_start)
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, "", ""))
    monkeypatch.setattr(run_nod, "build_bench_agent_with_jobs", fake_build)
    monkeypatch.setattr(run_nod, "settle_jobs", lambda agent, jobs, session_id, log: seen.update(settled=session_id))
    inst = {"instance_id": "psf__requests-863", "image": "img", "base_commit": "x", "problem_statement": "p"}

    run_nod.solve_instance(inst, model="m", base_url="u", max_turns=1, workdir=str(work),
                           traces_dir=str(traces), extras=True)

    # Job logs and subagent sessions are named after the session, so it must not
    # reveal the instance.
    assert "863" not in seen["session"] and "requests" not in seen["session"]
    assert seen["settled"] == seen["session"] and seen["shutdown"]
    assert seen["jobs_dir"] == os.path.join(seen["scratch_dir"], "jobs")
    assert sorted(p.name for p in traces.iterdir()) == ["psf__requests-863.jsonl", "psf__requests-863_job-1.jsonl"]


def test_extras_flag_is_off_by_default_and_needs_nod_extras(monkeypatch):
    import pytest

    run_nod = _load_run_nod_module()
    monkeypatch.setattr(run_nod, "load_instances", lambda *a: [])
    with pytest.raises(SystemExit):
        run_nod.main(["--dry-run"])  # no instances: exits before solving, extras never imported
    monkeypatch.setitem(__import__("sys").modules, "nod_extras.jobs", None)  # as if not installed
    with pytest.raises(SystemExit) as exc:
        run_nod.main(["--extras", "--dry-run"])
    assert exc.value.code == 2
