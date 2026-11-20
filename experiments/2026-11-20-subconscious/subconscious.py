"""A second agent that watches Nod's transcript and now and then slips it a thought.

It runs on its own thread and reads the session's JSONL file the way any
outside observer would. Whenever the transcript has moved on, it shows the
recent part to its own model and asks for either nothing or one short thought.
A thought goes into Nod's inbox, phrased as something Nod feels or that just
came to it, so the loop delivers it before Nod's next provider call. It never
wakes Nod: a woken turn's reply goes to the user, and Nod answered a late
thought as if the user had said it. A thought that forms after Nod's final
answer waits and surfaces at the start of its next turn, before the user's
message.

Two things keep it from running behind Nod:
- The file alone lags: Nod's reply is only persisted once its tool calls
  finish. `watch` wraps Nod's provider so each reply that calls tools is seen
  the moment it arrives, reasoning included.
- Thinking takes about as long as Nod's own step, so Nod is often reasoning
  about the same thing in parallel. A finished thought waits for any call Nod
  has in flight (it couldn't be delivered before Nod's next call anyway); if
  Nod produced anything meanwhile, the thought is dropped as stale and the
  subconscious looks again with Nod's new reasoning in view. That look is
  shown the dropped thought and asked whether Nod has dealt with it, so a
  point Nod hasn't reached isn't lost just because Nod was quicker.
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
mention it. Treat it like any hunch: often worth a look, sometimes wrong.
One can also come to you between messages and surface just before the user's
next one; it's still yours, not part of what they said."""

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

# Added to the transcript on the look after a thought was dropped as stale.
CARRIED = """

---
Just before Nod's latest step you were about to think:
{kind}: {text}
If what Nod has done since already deals with that, let it go. If not, it \
still stands: say it again, reworded if that helps."""

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
        poll_seconds: float = 0.5,
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
        # The last thought dropped as stale, shown to the next look.
        self._carry: tuple[str, str] | None = None
        # Nod's tool-calling replies not yet in the file, in arrival order.
        self._live: list[Message] = []
        self._live_lock = threading.Lock()
        # Nod's provider calls: how many are running, how many have finished.
        self._inflight = 0
        self._finished = 0
        self._stop = threading.Event()
        self._busy = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="subconscious", daemon=True)

    def start(self) -> Subconscious:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def watch(self, provider: Provider) -> Provider:
        """Wrap Nod's provider so its replies reach us before they're persisted."""

        return _Watched(provider, self)

    def _calling(self) -> None:
        with self._live_lock:
            self._inflight += 1

    def _heard(self, reply: Message | None) -> None:
        with self._live_lock:
            # Only replies that call tools wait to be persisted; the rest
            # (final answers, compaction summaries, dropped empty replies)
            # are written at once or never.
            if reply is not None and reply.tool_calls:
                self._live.append(reply)
            self._inflight -= 1
            self._finished += 1

    def _wait_for_nod(self) -> None:
        while not self._stop.is_set():
            with self._live_lock:
                if not self._inflight:
                    return
            time.sleep(0.1)

    def _snapshot(self) -> list[Message]:
        """The transcript as it stands: the file plus replies not yet in it."""

        messages = self.memory.load(self.session_id)
        with self._live_lock:
            # The memory sets a message's id when it persists it.
            self._live = [m for m in self._live if m.id is None]
            live = list(self._live)
        return messages + live

    def settled(self) -> bool:
        """True once it has looked at the transcript as it stands now."""

        if self._busy.is_set():
            return False
        messages = self._snapshot()
        return not messages or _mark(messages) == self._seen

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
        finished = self._finished
        messages = self._snapshot()
        if not messages or _mark(messages) == self._seen:
            return
        self._busy.set()
        try:
            mark = self._seen = _mark(messages)
            if not self._worth_a_look(messages):
                return
            carried, self._carry = self._carry, None
            transcript = render(messages)
            if carried is not None:
                transcript += CARRIED.format(kind=carried[0], text=carried[1])
            reply = self.provider.complete([system(PROMPT), user(transcript)])
            kind, text = parse(reply.text)
            stale = False
            if kind is not None:
                self._wait_for_nod()
                stale = self._finished != finished or _mark(self._snapshot()) != mark
            self._log(mark, reply, kind, text, stale, carried)
            if stale:
                # Nod moved on while this formed and may already be there.
                # Look again straight away, with what it did in view and
                # this thought carried along to be kept or let go.
                self._seen = None
                self._carry = (kind, text)
                _say(f"~ (stale, rechecking) {kind}: {text}")
            elif kind is not None:
                self.inbox.post(
                    self.session_id, FRAMES[kind].format(text=text), wake=False
                )
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

    def _log(
        self,
        at: str,
        reply: Message,
        kind: str | None,
        text: str,
        stale: bool,
        carried: tuple[str, str] | None,
    ) -> None:
        record = {
            "at": at,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "kind": kind,
            "text": text,
            "stale": stale,
            "carried": f"{carried[0]}: {carried[1]}" if carried else None,
            "raw": reply.text,
            "usage": reply.meta.get("usage"),
        }
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


class _Watched:
    """Nod's provider, passing each reply to the subconscious on the way out."""

    def __init__(self, inner: Provider, sub: Subconscious) -> None:
        self.inner = inner
        self.sub = sub

    def complete(self, messages, tools=None, on_token=None) -> Message:
        self.sub._calling()
        reply = None
        try:
            reply = self.inner.complete(messages, tools, on_token)
            return reply
        finally:
            self.sub._heard(reply)


def _mark(messages: list[Message]) -> str:
    """Identifies where the transcript has got to."""

    last = messages[-1]
    # A live reply has no id yet. Once persisted its tool results follow it,
    # so the mark changes again then, which is what we want.
    return last.id or f"live:{id(last)}"


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
