"""Default wiring: assemble the parts and hand them to a gateway.

This is the one place that knows which concrete implementations to use. Swapping a
brain, store, or channel happens by editing the constructors here (or importing
`build_agent` and overriding from your own script).
"""

from __future__ import annotations

import argparse
import json
import secrets
import signal
import sys
import threading
import time
from pathlib import Path

from nod import config as config_module
from nod.agent import Agent
from nod.builtins import builtin_tools
from nod.compaction import CompactionConfig
from nod.config import Config
from nod.gateway import CLIGateway, Gateway
from nod.inbox import InMemoryInbox
from nod.memory import JSONFileMemory
from nod.provider import OpenAICompatibleProvider, ProviderError
from nod.sandbox import DockerSandbox, SandboxError, parse_mount
from nod.skills import SkillLoader
from nod.tools import ALWAYS_ACCEPT, ToolRegistry


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
        Jobs = None

    # With the sandbox on, every built-in tool (the subagents' and shell jobs
    # too) runs in one container; Nod itself stays on the host.
    sandbox = _build_sandbox(cfg, with_jobs=Jobs is not None) if cfg.sandbox else None

    def plain_tools():
        return sandbox.tools() if sandbox else builtin_tools()

    def with_sandbox_note(soul: str) -> str:
        # Tell the model about limits it can't see, like a cut network.
        note = sandbox.prompt_note() if sandbox else ""
        return f"{soul.rstrip()}\n\n{note}" if note else soul

    registry = ToolRegistry(plain_tools())

    if Jobs is not None:

        def child_agent(child_provider) -> Agent:
            # A subagent gets the parent's brain, store and skills, a
            # task-focused soul, the plain tools and no job tools: no nesting,
            # so a parent's spend is bounded by its own limits. It takes the
            # parent's pre-tool hook as it stands when the child starts, so a
            # gate swapped in after build_agent covers subagents too.
            return Agent(
                provider=child_provider,
                registry=ToolRegistry(plain_tools()),
                memory=memory,
                skills=skills,
                soul=with_sandbox_note(SUBAGENT_SOUL),
                max_turns=cfg.max_turns,
                compaction=compaction,
                pre_tool_hook=agent.pre_tool_hook,
            )

        jobs = Jobs(
            inbox,
            [
                ShellJobKind(argv=sandbox.job_argv, kill=sandbox.job_kill)
                if sandbox
                else ShellJobKind(),
                AgentJobKind(provider, child_agent),
            ],
            jobs_dir=cfg.jobs_dir,
        )
        for t in job_tools(jobs):
            registry.register(t)

    # Every tool call passes `pre_tool_hook` first. Core accepts everything;
    # to gate calls, set `agent.pre_tool_hook` (nod-extras does the judging).
    agent = Agent(
        provider=provider,
        registry=registry,
        memory=memory,
        skills=skills,
        soul=with_sandbox_note(cfg.soul()),
        max_turns=cfg.max_turns,
        compaction=compaction,
        inbox=inbox,
        pre_tool_hook=ALWAYS_ACCEPT,
    )
    if sandbox:
        sandbox.start()
    return agent


def _build_sandbox(cfg: Config, *, with_jobs: bool) -> DockerSandbox:
    mounts = [parse_mount(m, cfg.base_dir) for m in cfg.sandbox_mounts]
    if with_jobs:
        # The job tools tell the model to read_file a job's log, so the logs
        # have to be visible in the container too.
        jobs_dir = Path(cfg.jobs_dir).expanduser().resolve()
        jobs_dir.mkdir(parents=True, exist_ok=True)
        mounts.append(f"{jobs_dir}:{jobs_dir}:ro")
    return DockerSandbox(
        workspace=cfg.resolve(cfg.sandbox_workspace),
        mounts=mounts,
        network=cfg.sandbox_network,
        image=cfg.sandbox_image,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nod", description="A thin agent core.")
    parser.add_argument("-c", "--config", help="Path to a config.toml file.")
    parser.add_argument(
        "-m", "--model", help="Model id to use instead of the config's (e.g. openai/gpt-5)."
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        metavar="N",
        help="Provider round-trips one turn may take, instead of the config's.",
    )
    sandbox_g = parser.add_argument_group(
        "sandbox", "Run the tools in a Docker container. Any of these turns it on."
    )
    sandbox_g.add_argument("--sandbox", action="store_true", help="Turn the sandbox on.")
    sandbox_g.add_argument(
        "--workspace", help="Host dir mounted at /workspace (default: the current dir)."
    )
    sandbox_g.add_argument(
        "--mount",
        action="append",
        default=[],
        metavar="HOST[:CONTAINER][:ro]",
        help="Extra mount, at the same path unless CONTAINER is given. Repeatable.",
    )
    sandbox_g.add_argument(
        "--no-network", action="store_true", help="Give the container no network."
    )
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

    run_p = sub.add_parser(
        "run",
        help="One shot: carry out one set of instructions, print the reply, exit.",
        epilog=RUN_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument(
        "text", nargs="*", help="The instructions (joined with spaces)."
    )
    run_p.add_argument(
        "-f",
        "--file",
        help="Read the instructions from this file ('-' for stdin). "
        "With text as well, the text goes after the file's contents.",
    )
    run_p.add_argument(
        "--session",
        default=None,
        help="Session id (default: a fresh one, printed to stderr at the end).",
    )

    args = parser.parse_args(argv)

    if args.command == "run":
        try:
            instructions = _read_instructions(args)
        except (OSError, ValueError) as exc:
            print(f"[run] {exc}", file=sys.stderr)
            return 2

    cfg = config_module.load(args.config)
    if args.model:
        cfg.model = args.model
    if args.max_turns is not None:
        cfg.max_turns = args.max_turns

    # A plain SIGTERM would skip atexit, and with it the hooks that remove the
    # sandbox container and kill background jobs. Turn it into a normal exit
    # instead. Installed before the agent is built, because building it starts
    # the container (the HTTP gateway swaps in its own handlers while it runs).
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    try:
        _apply_sandbox_args(cfg, args)
        agent = build_agent(cfg)
    except (ProviderError, SandboxError) as exc:
        print(f"[config] {exc}", file=sys.stderr)
        return 2

    if args.command == "serve":
        return _serve(cfg, agent, args)

    if args.command == "run":
        return _run_once(agent, instructions, args.session)

    gateway: Gateway = CLIGateway(session_id=args.session)
    gateway.run(agent)
    return 0


def _read_instructions(args: argparse.Namespace) -> str:
    parts = []
    if args.file == "-":
        parts.append(sys.stdin.read())
    elif args.file:
        parts.append(Path(args.file).expanduser().read_text())
    parts.append(" ".join(args.text))
    instructions = "\n\n".join(p.strip() for p in parts if p.strip())
    if not instructions:
        raise ValueError("no instructions: pass them as arguments or with -f FILE")
    return instructions


RUN_EPILOG = """\
The reply goes to stdout. Two lines go to stderr at the end, on failure too:
  [usage {...}]    provider calls, tokens and cost of this run, as JSON
  [session <id>]   the session, to pick up with `nod chat --session <id>`

exit status:
  0  Nod replied. A turn cut short by the max_turns limit also exits 0, and
     its reply is "(stopped: hit the max_turns limit before finishing)".
  1  the turn failed (the provider, usually); stderr has an [error] line
  2  nothing ran: no instructions, an unreadable file, or a config, API key
     or sandbox problem
"""


def _run_once(agent: Agent, instructions: str, session_id: str | None) -> int:
    """One turn on `instructions`, then out: the reply goes to stdout, what the
    run used and the session id to stderr, so a caller can cost the run and the
    conversation can be picked up with `nod chat`.

    A job that finishes during the last provider call misses that turn, so its
    event gets a wake-up before exit. Jobs still running when this returns are
    killed at exit; a task that needs them should wait for them itself.
    """
    # A suffix keeps two runs started in the same second apart.
    session_id = session_id or time.strftime("run-%Y%m%d-%H%M%S-") + secrets.token_hex(2)
    try:
        reply = agent.send(instructions, session_id=session_id)
        while (woken := agent.wake(session_id)) is not None:
            reply = woken
    except Exception as exc:  # noqa: BLE001 - report it, don't dump a traceback
        print(f"[error] {exc}", file=sys.stderr)
        _print_run_footer(agent, session_id)
        return 1
    print(reply)
    _print_run_footer(agent, session_id)
    return 0


def _print_run_footer(agent: Agent, session_id: str) -> None:
    # The provider's own totals, so compaction and subagent calls are in them.
    # A provider that keeps none (a custom one) gets no usage line.
    usage = getattr(agent.provider, "usage", None)
    if usage:
        print(f"[usage {json.dumps(usage)}]", file=sys.stderr)
    print(f"[session {session_id}]", file=sys.stderr)


def _apply_sandbox_args(cfg: Config, args: argparse.Namespace) -> None:
    if args.sandbox or args.workspace or args.mount or args.no_network:
        cfg.sandbox = True
    if args.workspace:
        cfg.sandbox_workspace = str(Path(args.workspace).expanduser().resolve())
    # Paths on the command line are relative to the current dir, not the config.
    cfg.sandbox_mounts = cfg.sandbox_mounts + [parse_mount(m, Path.cwd()) for m in args.mount]
    if args.no_network:
        cfg.sandbox_network = False


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
