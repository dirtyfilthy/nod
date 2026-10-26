"""Default wiring: assemble the parts and hand them to a gateway.

This is the one place that knows which concrete implementations to use. Swapping a
brain, store, or channel happens by editing the constructors here (or importing
`build_agent` and overriding from your own script).
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from nod import config as config_module
from nod.agent import Agent
from nod.builtins import builtin_tools
from nod.compaction import CompactionConfig
from nod.config import Config
from nod.gateway import CLIGateway, Gateway
from nod.inbox import InMemoryInbox
from nod.memory import JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.skills import SkillLoader
from nod.tools import ToolRegistry


def build_agent(cfg: Config) -> Agent:
    provider = OpenAICompatibleProvider(
        model=cfg.model,
        base_url=cfg.base_url,
        provider_routing=cfg.provider_routing,
        reasoning=cfg.reasoning,
    )
    memory = JSONFileMemory(cfg.memory_dir)
    skills = SkillLoader(cfg.resolved_skills_dirs())
    inbox = InMemoryInbox()
    registry = ToolRegistry(builtin_tools())
    compaction = CompactionConfig(
        enabled=cfg.compaction_enabled,
        context_window=cfg.context_window,
        reserve_tokens=cfg.reserve_tokens,
        keep_recent_tokens=cfg.keep_recent_tokens,
    )

    # Background jobs live in nod-extras. With it on the path, the job tools
    # join the registry; without it Nod runs with the built-ins alone.
    try:
        from nod_extras.jobs import SUBAGENT_SOUL, AgentJobKind, Jobs, ShellJobKind, job_tools
    except ImportError:
        pass
    else:

        def child_agent(child_provider) -> Agent:
            # A subagent gets the parent's brain, store and skills, a
            # task-focused soul, the plain tools and no job tools: no nesting,
            # so a parent's spend is bounded by its own limits.
            return Agent(
                provider=child_provider,
                registry=ToolRegistry(builtin_tools()),
                memory=memory,
                skills=skills,
                soul=SUBAGENT_SOUL,
                max_turns=cfg.max_turns,
                compaction=compaction,
            )

        jobs = Jobs(
            inbox,
            [ShellJobKind(), AgentJobKind(provider, child_agent)],
            jobs_dir=cfg.jobs_dir,
        )
        for t in job_tools(jobs):
            registry.register(t)

    return Agent(
        provider=provider,
        registry=registry,
        memory=memory,
        skills=skills,
        soul=cfg.soul(),
        max_turns=cfg.max_turns,
        compaction=compaction,
        inbox=inbox,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nod", description="A thin agent core.")
    parser.add_argument("-c", "--config", help="Path to a config.toml file.")
    # Default action (no subcommand) is the interactive chat REPL.
    parser.set_defaults(command="chat", session="cli", host=None, port=None)

    sub = parser.add_subparsers(dest="command")

    chat_p = sub.add_parser("chat", help="Interactive CLI chat (default).")
    chat_p.add_argument(
        "--session", default="cli", help="Conversation/session id (default: cli)."
    )

    serve_p = sub.add_parser("serve", help="Run nod as a long-lived HTTP service.")
    serve_p.add_argument("--host", default=None, help="Bind address (default: config).")
    serve_p.add_argument(
        "--port", type=int, default=None, help="Bind port (default: config)."
    )

    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    try:
        agent = build_agent(cfg)
    except ProviderError as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        return _serve(cfg, agent, args)

    # A plain SIGTERM would skip atexit, and with it the hook that kills
    # background jobs. Turn it into a normal exit instead (the HTTP gateway
    # handles its own signals).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    gateway: Gateway = CLIGateway(session_id=args.session)
    gateway.run(agent)
    return 0


def _serve(cfg: Config, agent: Agent, args: argparse.Namespace) -> int:
    token = cfg.api_token()
    if not token:
        print(
            "[config] refusing to serve without auth: set $NOD_API_TOKEN to a "
            "secret value first.",
            file=sys.stderr,
        )
        return 2

    from nod.server import HTTPGateway

    gateway: Gateway = HTTPGateway(
        host=args.host or cfg.host,
        port=args.port or cfg.port,
        token=token,
    )
    gateway.run(agent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
