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
    time.sleep(0.3)  # overlap with the other workers
    return {"instance_id": inst["instance_id"], "model_patch": os.getcwd(), "pid": os.getpid()}


def test_solve_all_parallel_workers_keep_their_own_cwd(tmp_path):
    run_nod = _load_run_nod_module()
    rows = []
    for i in range(4):
        d = tmp_path / f"inst{i}"
        d.mkdir()
        rows.append({"instance_id": f"inst{i}", "dir": str(d)})

    cwd = os.getcwd()
    results = dict(
        (inst["instance_id"], pred)
        for inst, pred in run_nod.solve_all(rows, _chdir_and_report, workers=4)
    )

    assert os.getcwd() == cwd
    assert set(results) == {r["instance_id"] for r in rows}
    for row in rows:
        assert os.path.realpath(results[row["instance_id"]]["model_patch"]) == os.path.realpath(row["dir"])
    assert len({pred["pid"] for pred in results.values()}) > 1


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
