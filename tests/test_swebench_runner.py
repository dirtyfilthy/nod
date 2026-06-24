import importlib.util
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
    assert "Available skills" in prompt
    assert "plan: Make a concise implementation plan" in prompt
    assert "read_skill" in agent.registry
