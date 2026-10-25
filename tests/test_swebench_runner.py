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

    def fake_start(image, name, host_dir, log=print):
        seen.update(name=name, host_dir=host_dir)
        return False  # stop before any docker work

    monkeypatch.setattr(run_nod, "start_container", fake_start)
    inst = {"instance_id": "psf__requests-863", "image": "img", "base_commit": "x", "problem_statement": "p"}

    run_nod.solve_instance(inst, model="m", base_url="u", max_turns=1, workdir=str(tmp_path), traces_dir=None)

    for value in (seen["name"], os.path.relpath(seen["host_dir"], tmp_path)):
        assert "863" not in value and "requests" not in value


def test_pull_image_waits_out_docker_hub_rate_limit(monkeypatch):
    import subprocess

    run_nod = _load_run_nod_module()
    results = iter([
        subprocess.CompletedProcess([], 1, "", "toomanyrequests: You have reached your unauthenticated pull rate limit."),
        subprocess.CompletedProcess([], 1, "", "toomanyrequests: You have reached your unauthenticated pull rate limit."),
        subprocess.CompletedProcess([], 0, "", ""),
    ])
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: next(results))
    slept = []

    assert run_nod.pull_image("img", log=lambda m: None, sleep=slept.append)
    assert slept == [run_nod.PULL_RATE_LIMIT_WAIT_SECONDS] * 2


def test_pull_image_gives_up_on_other_errors_and_after_max_wait(monkeypatch):
    import subprocess

    run_nod = _load_run_nod_module()
    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "manifest unknown"))
    assert not run_nod.pull_image("img", log=lambda m: None, sleep=lambda s: None)

    monkeypatch.setattr(run_nod, "_run", lambda cmd, **kw: subprocess.CompletedProcess([], 1, "", "toomanyrequests"))
    slept = []
    assert not run_nod.pull_image("img", log=lambda m: None, sleep=slept.append)
    assert sum(slept) == run_nod.PULL_RATE_LIMIT_MAX_WAIT_SECONDS
