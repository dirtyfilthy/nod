from __future__ import annotations

from nod.config import Config, load


def test_default_routing_pins_deepseek_without_fallbacks():
    assert Config().provider_routing == {
        "order": ["deepseek"],
        "allow_fallbacks": False,
    }


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
