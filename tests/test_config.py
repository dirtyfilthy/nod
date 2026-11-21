from __future__ import annotations

from nod.config import Config, load
from nod.provider import routing_for


def test_default_routing_pins_deepseek_without_fallbacks():
    assert Config().provider_routing == {
        "order": ["deepseek"],
        "allow_fallbacks": False,
        "model_prefix": "deepseek/",
    }


def test_default_routing_covers_the_default_model_only():
    cfg = Config()
    pin = {"order": ["deepseek"], "allow_fallbacks": False}
    assert routing_for(cfg.model, cfg.provider_routing) == pin
    assert routing_for("anthropic/claude-haiku-5.5", cfg.provider_routing) == {}


def test_load_keeps_routing_scope_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('provider_routing = { order = ["deepinfra"], models = ["a/b"] }\n')
    assert load(path).provider_routing == {"order": ["deepinfra"], "models": ["a/b"]}


def test_load_overrides_provider_routing(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('provider_routing = { order = ["deepinfra/fp8"] }\n')
    assert load(path).provider_routing == {"order": ["deepinfra/fp8"]}


def test_load_empty_routing_disables_pinning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("provider_routing = {}\n")
    assert load(path).provider_routing == {}


def test_default_reasoning_sends_nothing():
    assert Config().reasoning == {}


def test_load_reads_reasoning(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('reasoning = { effort = "high" }\n')
    assert load(path).reasoning == {"effort": "high"}


def test_jobs_dir_default_and_override(tmp_path):
    from nod import config as config_module
    from nod.config import Config

    assert Config().jobs_dir == "/var/tmp/nod-jobs"
    (tmp_path / "config.toml").write_text('jobs_dir = "/tmp/elsewhere"\n', encoding="utf-8")
    assert config_module.load(tmp_path / "config.toml").jobs_dir == "/tmp/elsewhere"
