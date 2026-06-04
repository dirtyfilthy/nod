"""Default wiring: assemble the parts and hand them to a gateway.

This is the one place that knows which concrete implementations to use. Swapping a
brain, store, or channel happens by editing the constructors here (or importing
`build_agent` and overriding from your own script).
"""

from __future__ import annotations

import argparse
import sys

from nod import config as config_module
from nod.agent import Agent
from nod.config import Config
from nod.gateway import CLIGateway, Gateway
from nod.memory import JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.skills import SkillLoader
from nod.tools import ToolRegistry


def build_agent(cfg: Config) -> Agent:
    provider = OpenAICompatibleProvider(model=cfg.model, base_url=cfg.base_url)
    memory = JSONFileMemory(cfg.memory_dir)
    skills = SkillLoader(cfg.resolved_skills_dirs())
    registry = ToolRegistry()
    return Agent(
        provider=provider,
        registry=registry,
        memory=memory,
        skills=skills,
        soul=cfg.soul(),
        max_turns=cfg.max_turns,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nod", description="A thin agent core.")
    parser.add_argument("-c", "--config", help="Path to a config.toml file.")
    parser.add_argument(
        "--session", default="cli", help="Conversation/session id (default: cli)."
    )
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    try:
        agent = build_agent(cfg)
    except ProviderError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    gateway: Gateway = CLIGateway(session_id=args.session)
    gateway.run(agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
