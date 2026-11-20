"""Nod with a subconscious.

  run.py                     chat with it (thoughts can wake Nod while idle)
  run.py --task "..."        one task, then let any late thoughts play out
  run.py --demo              the task in demo/, run in a fresh copy under runs/
  run.py --demo --off        same, no subconscious (the baseline)
  run.py --demo --task "..." the demo files with a different instruction
  run.py --sub-model M       a different model for the subconscious

Thoughts print to stderr in dim magenta as they happen; show.py replays a session.
"""

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import lab  # noqa: E402
from subconscious import SOUL_NOTE, Subconscious  # noqa: E402

from nod.gateway import CLIGateway  # noqa: E402
from nod.provider import OpenAICompatibleProvider  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--off", action="store_true", help="no subconscious, no soul note")
    p.add_argument("--sub-model", help="model for the subconscious (default: Nod's)")
    p.add_argument("--min-gap", type=int, default=3)
    p.add_argument("--session", default=time.strftime("s-%Y%m%d-%H%M%S"))
    args = p.parse_args()

    cfg = lab.config(HERE)
    agent = lab.agent(cfg)
    sid = args.session
    print(f"[session {sid}{' (no subconscious)' if args.off else ''}]", file=sys.stderr)

    task = args.task
    if args.demo:
        sandbox = HERE / "runs" / "sandbox" / sid
        shutil.copytree(HERE / "demo", sandbox, ignore=shutil.ignore_patterns("TASK.md"))
        os.chdir(sandbox)
        task = task or (HERE / "demo" / "TASK.md").read_text()
        print(f"[working in {sandbox}]", file=sys.stderr)

    sub = None
    if not args.off:
        agent.soul = f"{agent.soul}\n\n{SOUL_NOTE}"
        model = args.sub_model or cfg.model
        sub = Subconscious(
            provider=OpenAICompatibleProvider(
                model=model,
                base_url=cfg.base_url,
                # The pinned routing only makes sense for Nod's own model.
                provider_routing=cfg.provider_routing if model == cfg.model else {},
                reasoning=cfg.reasoning,
            ),
            memory_dir=cfg.memory_dir,
            inbox=agent.inbox,
            session_id=sid,
            log_path=HERE / "runs" / "thoughts" / f"{sid}.jsonl",
            min_gap=args.min_gap,
        ).start()

    try:
        if task is None:
            CLIGateway(session_id=sid).run(agent)
            return 0
        agent.send(task, session_id=sid, on_token=lab.stream)
        print()
        # A thought that lands after Nod's final reply gets a turn of its own,
        # as it would in a chat; agent.max_wakeups bounds how often.
        while sub is not None:
            sub.wait_settled()
            if agent.wake(sid, on_token=lab.stream) is None:
                break
            print()
        return 0
    finally:
        if sub is not None:
            sub.stop()


if __name__ == "__main__":
    raise SystemExit(main())
