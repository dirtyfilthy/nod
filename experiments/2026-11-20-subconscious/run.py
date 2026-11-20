"""Nod with a subconscious.

  run.py                     chat with it
  run.py --task "..."        one task; a late thought is shown, not delivered
  run.py --demo              the task in demo/, run in a fresh copy under runs/
  run.py --demo --off        same, no subconscious (the baseline)
  run.py --demo --task "..." the demo files with a different instruction
  run.py --sub-model M       a different model for the subconscious
  run.py --sync              lockstep: it looks before each of Nod's steps
                             and Nod waits, instead of racing it in real time
  run.py --grace 3           real time, but Nod waits up to 3s before a step
                             for a look that's under way

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


class CappedProvider(OpenAICompatibleProvider):
    """The subconscious's brain, with a cap on how much it may write.

    Its answer is one line, but twice it wrote 9-18k chars of invented
    transcript instead. Normal looks use under 800 completion tokens
    (reasoning included, max seen 992 of reasoning), so 1500 stops a runaway
    without touching a real answer. A capped reply parses as nothing.
    """

    def __init__(self, *args, max_tokens: int = 1500, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.max_tokens = max_tokens

    def _payload(self, messages, tools):
        payload = super()._payload(messages, tools)
        payload["max_tokens"] = self.max_tokens
        return payload


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task")
    p.add_argument("--demo", action="store_true")
    p.add_argument("--off", action="store_true", help="no subconscious, no soul note")
    p.add_argument("--sub-model", help="model for the subconscious (default: Nod's)")
    p.add_argument("--min-gap", type=int, default=3)
    p.add_argument("--sync", action="store_true", help="look before each step, Nod waiting")
    p.add_argument(
        "--grace", type=float, default=0.0,
        help="real time: seconds Nod may wait before a step for a look under way",
    )
    p.add_argument("--session", default=time.strftime("s-%Y%m%d-%H%M%S"))
    args = p.parse_args()

    cfg = lab.config(HERE)
    agent = lab.agent(cfg)
    sid = args.session
    mode = (
        " (no subconscious)" if args.off
        else " (lockstep)" if args.sync
        else f" (real time, grace {args.grace:g}s)" if args.grace
        else ""
    )
    print(f"[session {sid}{mode}]", file=sys.stderr)

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

        def brain(max_tokens: int) -> CappedProvider:
            return CappedProvider(
                model=model,
                base_url=cfg.base_url,
                # The pinned routing only makes sense for Nod's own model.
                provider_routing=cfg.provider_routing if model == cfg.model else {},
                reasoning=cfg.reasoning,
                max_tokens=max_tokens,
            )

        sub = Subconscious(
            provider=brain(1500),
            # The evidence check reasons for ~1.2-1.4k tokens and came back
            # empty under the 1500 cap.
            checker=brain(4000),
            memory_dir=cfg.memory_dir,
            inbox=agent.inbox,
            session_id=sid,
            log_path=HERE / "runs" / "thoughts" / f"{sid}.jsonl",
            min_gap=args.min_gap,
            grace=args.grace,
        )
        if args.sync:
            sub.attach(agent)
        else:
            sub.start()
            agent.provider = sub.watch(agent.provider)
            sub.wait_at_gate(agent)

    try:
        if task is None:
            CLIGateway(session_id=sid).run(agent)
            return 0
        started = time.monotonic()
        agent.send(task, session_id=sid, on_token=lab.stream)
        print()
        print(f"[turn took {time.monotonic() - started:.0f}s]", file=sys.stderr)
        if sub is not None:
            # Its last look, at Nod's answer. The thought never wakes Nod; in
            # a chat it would surface before the next message. In lockstep
            # that look would only come with the next message, so take it now.
            if args.sync:
                sub.look_now(sub.memory.load(sid))
            else:
                sub.wait_settled()
            if sub.gate_waits:
                waits = sub.gate_waits
                print(
                    f"[grace: Nod waited {sum(w for w, _ in waits):.0f}s over {len(waits)} steps; "
                    + ", ".join(f"{o} {sum(1 for _, x in waits if x == o)}" for o in ("caught up", "timed out", "behind"))
                    + "]",
                    file=sys.stderr,
                )
            for event in agent.inbox.pending(sid):
                print(f"[held for the next turn] {event.text}", file=sys.stderr)
        return 0
    finally:
        if sub is not None:
            sub.stop()


if __name__ == "__main__":
    raise SystemExit(main())
