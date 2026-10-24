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
