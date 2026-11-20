"""A second agent that watches Nod's transcript and now and then slips it a thought.

It runs on its own thread and reads the session's JSONL file the way any
outside observer would. Whenever the transcript has moved on, it shows the
recent part to its own model and asks for either nothing or one short thought.
A thought goes into Nod's inbox, phrased as something Nod feels or that just
came to it, so the loop delivers it before Nod's next provider call (or wakes
Nod if it's idle and a gateway is listening).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

from nod.inbox import Inbox
from nod.memory import JSONFileMemory
from nod.messages import Message, system, user
from nod.provider import Provider

# How a thought reads when it surfaces in Nod. The subconscious picks the kind
# and the words; the framing is fixed so it's the same every time.
FRAMES = {
    "doubt": "*A flicker of uncertainty: {text}*",
    "idea": "*An idea surfaces, unbidden: {text}*",
    "recall": "*Something tugs at your memory: {text}*",
}

# Added to Nod's soul so it doesn't mistake a thought for the user talking.
SOUL_NOTE = """\
## Intuition

Now and then a thought surfaces in you while you work: a flicker of
uncertainty, an idea that just came to you, something tugging at your memory.
It arrives in italics. It's yours, not the user's, so don't answer it or
mention it. Treat it like any hunch: often worth a look, sometimes wrong."""

PROMPT = """\
You are the subconscious of an AI agent called Nod. You watch it work: its \
conversation with the user, the tools it calls and what they return, and its \
private reasoning. Nod can't hear you directly. What you say surfaces in its \
mind as a passing feeling or a thought that just came to it.

Stay silent most of the time. Speak only when you notice something Nod seems \
to be missing that would change what it does next, for example:
- an assumption it hasn't checked, or a claim the evidence doesn't support
- something in a tool result it skimmed past
- going round in circles, or digging deeper into an approach that isn't working
- something the user asked for that it has lost track of
- a simpler or more direct path

How to speak:
- One thought, under 25 words, addressed to Nod as "you" or as a bare question.
- A hunch, not an instruction. Never a plan, never code.
- Don't repeat a thought you've already had; they appear in the transcript as THOUGHT.
- Don't tell Nod what it already knows or is already doing.

Reply with exactly one line, one of:
nothing
doubt: <thought>
idea: <thought>
recall: <thought>"""

# Budget for the transcript the subconscious sees, in characters, newest kept.
WINDOW_CHARS = 40_000
RESULT_CHARS = 2_000
REASONING_CHARS = 1_500


class Subconscious:
    def __init__(
        self,
        *,
        provider: Provider,
        memory_dir: str | Path,
        inbox: Inbox,
        session_id: str,
        log_path: str | Path,
        min_gap: int = 3,
        poll_seconds: float = 1.0,
    ) -> None:
        self.provider = provider
        # A reader of its own: sharing the agent's JSONFileMemory would reset
        # the ids it tracks as persisted and make it write duplicates.
        self.memory = JSONFileMemory(memory_dir)
        self.inbox = inbox
        self.session_id = session_id
        self.log_path = Path(log_path)
        # Messages Nod must add after a thought before the next one may come.
        self.min_gap = min_gap
        self.poll_seconds = poll_seconds
        self._seen: str | None = None
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="subconscious", daemon=True)

    def start(self) -> Subconscious:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def settled(self) -> bool:
        """True once it has looked at the transcript as it stands now."""

        if self._busy.is_set():
            return False
        messages = self.memory.load(self.session_id)
        return not messages or messages[-1].id == self._seen

    def wait_settled(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while not self.settled() and time.monotonic() < deadline:
            time.sleep(0.2)

    # -- the watching loop -------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.wait(self.poll_seconds):
            try:
                self._look()
            except Exception as exc:  # noqa: BLE001 - never take Nod down with us
                _say(f"[subconscious error] {exc}")

    def _look(self) -> None:
        messages = self.memory.load(self.session_id)
        if not messages or messages[-1].id == self._seen:
            return
        self._busy.set()
        try:
            self._seen = messages[-1].id
            if not self._worth_a_look(messages):
                return
            reply = self.provider.complete(
                [system(PROMPT), user(render(messages))]
            )
            kind, text = parse(reply.text)
            self._log(messages[-1].id, reply, kind, text)
            if kind is not None:
                self.inbox.post(self.session_id, FRAMES[kind].format(text=text))
                _say(f"~ {kind}: {text}")
        finally:
            self._busy.clear()

    def _worth_a_look(self, messages: list[Message]) -> bool:
        if self.inbox.pending(self.session_id):
            return False  # a thought is already on its way
        since = 0
        for m in reversed(messages):
            if is_thought(m):
                return since >= self.min_gap
            since += 1
        return True

    def _log(self, at: str | None, reply: Message, kind: str | None, text: str) -> None:
        record = {
            "at": at,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "text": text,
            "raw": reply.text,
            "usage": reply.meta.get("usage"),
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


def is_thought(m: Message) -> bool:
    return m.kind == "event" and any(
        m.content.startswith(frame.split("{text}")[0]) for frame in FRAMES.values()
    )


def parse(raw: str) -> tuple[str | None, str]:
    for line in raw.strip().splitlines():
        line = line.strip().strip("`")
        if not line:
            continue
        head, _, rest = line.partition(":")
        kind = head.strip().lower()
        if kind in FRAMES and rest.strip():
            return kind, rest.strip()
        return None, ""
    return None, ""


def render(messages: list[Message]) -> str:
    """The transcript as the subconscious reads it, newest kept within the window."""

    lines: list[str] = []
    for m in messages:
        if m.kind == "compaction_summary":
            lines.append(f"EARLIER WORK (summary):\n{m.content}")
        elif is_thought(m):
            lines.append(f"THOUGHT (yours): {m.content.strip('*')}")
        elif m.kind == "event":
            lines.append(f"EVENT: {m.content}")
        elif m.role == "user":
            lines.append(f"USER: {m.content}")
        elif m.role == "assistant":
            if m.reasoning:
                lines.append(f"NOD (thinking): {_clip(m.reasoning, REASONING_CHARS)}")
            if m.content:
                lines.append(f"NOD: {m.content}")
            for call in m.tool_calls:
                args = json.dumps(call.arguments, ensure_ascii=False)
                lines.append(f"NOD calls {call.name}({_clip(args, RESULT_CHARS)})")
        elif m.role == "tool":
            lines.append(f"RESULT: {_clip(m.content, RESULT_CHARS)}")
    text = "\n\n".join(lines)
    if len(text) > WINDOW_CHARS:
        text = "[...earlier transcript cut...]\n\n" + text[-WINDOW_CHARS:]
    return text


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    half = n // 2
    return f"{s[:half]}\n[...{len(s) - n} chars cut...]\n{s[-half:]}"


def _say(line: str) -> None:
    # Dim magenta on stderr, so it's visible but apart from Nod's own output.
    sys.stderr.write(f"\033[2;35m{line}\033[0m\n")
    sys.stderr.flush()
