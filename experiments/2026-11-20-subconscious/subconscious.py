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
  point Nod hasn't reached isn't lost just because Nod was quicker. What
  that recheck says is delivered even if Nod has moved again: it is a step or
  two late, but it was formed knowing what Nod did, and rechecking it again
  only made chains of stale thoughts (17 in a row in one chat).
- `grace` lets Nod wait a few seconds before a step for a look that is under
  way (or about to start) on where it stands now. 0 is pure real time; a
  large value is close to lockstep.

That's the real-time mode (`start` + `watch`). Most thoughts in it lost the
race and went stale (36 of 48 over the first runs). Lockstep mode (`attach`)
does away with the race: the look runs before each of Nod's steps while Nod
waits, at the cost of a slower Nod.
"""

from __future__ import annotations

import json
import os
import re
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
    # The two that pull forward rather than back. Without them every thought
    # was a correction (40 doubts, 6 ideas, 0 recalls over the first runs), and
    # nothing ever told Nod it had enough or what was worth chasing.
    "curious": "*Something here catches your interest: {text}*",
    "settled": "*A quiet, settled feeling: {text}*",
}

# Added to Nod's soul so it doesn't mistake a thought for the user talking.
SOUL_NOTE = """\
## Intuition

Now and then a thought surfaces in you while you work: a flicker of
uncertainty, an idea that just came to you, something tugging at your memory,
a pull of interest, a settled sense that you have what you need.
It arrives in italics. It's yours, not the user's, so don't answer it or
mention it. Treat it like any hunch: often worth a look, sometimes wrong.
One can also come to you between messages and surface just before the user's
next one; it's still yours, not part of what they said."""

PROMPT = """\
You are the subconscious of an AI agent called Nod. You watch it work: its \
conversation with the user, the tools it calls and what they return, and its \
private reasoning. Nod can't hear you directly. What you say surfaces in its \
mind as a passing feeling or a thought that just came to it.

Stay silent most of the time. Speak only when you notice something that \
would change what Nod does next. It can pull Nod back:
- an assumption it hasn't checked, or a claim the evidence doesn't support
- something in a tool result it skimmed past
- going round in circles, or digging deeper into an approach that isn't working
- something the user asked for that it has lost track of
- a simpler or more direct path

Or it can pull Nod forward:
- curious: something in what Nod has already seen is more interesting or \
important than what it's chasing, and it hasn't noticed
- settled: the evidence Nod already has answers the question, but it keeps \
gathering, or it's about to re-check or hedge on something it has actually \
verified

A settled thought needs the same evidence as a doubt: point at what in the \
transcript already shows it. Never use it to cheer, agree or reassure; if Nod \
is simply doing fine, say nothing.

How to speak:
- One thought, under 25 words, addressed to Nod as "you" or as a bare question.
- A hunch, not an instruction. Never a plan, never code.
- Don't repeat a thought you've already had; they appear in the transcript as THOUGHT.
- Don't tell Nod what it already knows or is already doing.

Every thought needs evidence: copy the exact words from the transcript that \
it rests on, character for character, under 200 characters. Read that line \
again before you rely on it: what is the number, what is it counting, whose \
output is it? If you can't point at the words, say nothing.

Reply with either the single word:
nothing
or two lines, the first one of
doubt: <thought>
idea: <thought>
recall: <thought>
curious: <thought>
settled: <thought>
and the second
evidence: <exact words copied from the transcript>"""

# The second look a thought gets before it reaches Nod, from the same model
# but shown only the quote and what surrounds it. Its one job is misreadings,
# which slipped through twice ("On branch dirtyfilthy/..." when the output said
# main; a priority score of 178 read as 178 queued tasks).
CHECK = """\
Someone watching an AI agent's work wrote a hunch about it and quoted the \
transcript as evidence. Below is the hunch, the quote, and the part of the \
transcript around the quote.

Your only job is to catch misreadings of the quoted words: a number taken \
for the wrong quantity, a line read as saying something it doesn't, a label \
or column mistaken for another, words from one place taken as being about \
another. The hunch may also draw on parts of the transcript you can't see \
here, and may claim things this context can't show (such as what the agent \
hasn't done yet); those are not yours to judge, so let them pass. Say \
misread only when the quoted words, read in this context, don't say what the \
hunch takes them to say.

Hunch ({kind}): {text}

Quote: {quote}

Context:
{context}

Reply with exactly one line: either
holds
or
misread: <what it actually says, under 20 words>"""

# Shorter quotes (like `tags": []`) can be found almost anywhere and prove nothing.
QUOTE_MIN = 20
# Real-time looks allowed at once: the usual one, plus one the gate may add.
MAX_LOOKS = 2
CONTEXT_BEFORE = 1_500
CONTEXT_AFTER = 1_000

# Added to the transcript on the look after a thought was dropped as stale.
CARRIED = """

---
Just before Nod's latest step you were about to think:
{kind}: {text}
If what Nod has done since already deals with that, let it go. If not, it \
still stands: say it again, reworded if that helps."""

# Budget for the transcript the subconscious sees, in characters. When it's
# exceeded the start jumps forward to leave about half the budget, then stays
# put while the transcript grows again. A start that moved on every look (the
# newest N chars) changed the prompt's prefix each time and defeated the
# provider's prefix cache: on a long session ~2% of each look was cached.
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
        checker: Provider | None = None,
        grace: float = 0.0,
        live_delay: float = 1.5,
    ) -> None:
        self.provider = provider
        # Runs the misreading check. It reasons longer than a look does
        # (~1.2-1.4k tokens), so it may need a bigger output cap.
        self.checker = checker or provider
        # A reader of its own: sharing the agent's JSONFileMemory would reset
        # the ids it tracks as persisted and make it write duplicates.
        self.memory = JSONFileMemory(memory_dir)
        self.inbox = inbox
        self.session_id = session_id
        self.log_path = Path(log_path)
        # Messages Nod must add after a thought before the next one may come.
        self.min_gap = min_gap
        self.poll_seconds = poll_seconds
        # Seconds Nod may wait before a step for the subconscious to catch up
        # (real-time mode only; see `watch`).
        self.grace = grace
        # A reply still waiting on its tools is only looked at once it has
        # waited this long. Quick tools finish first, and a look started on
        # the bare reply would be stale the moment their results landed.
        self.live_delay = live_delay
        self._seen: str | None = None
        # "async" (start + watch) or "sync" (attach); recorded in the log.
        self.mode = "async"
        # The last thought dropped as stale, shown to the next look.
        self._carry: tuple[str, str] | None = None
        # Id of the first message in the window; see WINDOW_CHARS.
        self._window_from: str | None = None
        # Nod's tool-calling replies not yet in the file, in arrival order.
        self._live: list[tuple[Message, float]] = []
        self._live_lock = threading.Lock()
        # Set to make the loop look now instead of at its next poll.
        self._nudge = threading.Event()
        # (seconds Nod waited, "caught up" | "timed out" | "behind") per gate.
        self.gate_waits: list[tuple[float, str]] = []
        # Nod's provider calls: how many are running, how many have finished.
        self._inflight = 0
        self._finished = 0
        self._stop = threading.Event()
        # Guards the look bookkeeping: _seen, _carry, _window_from and these.
        self._lock = threading.RLock()
        self._gen = 0  # number of the newest look started
        self._active: dict[int, str] = {}  # looks in flight: number -> mark
        self._thread = threading.Thread(target=self._loop, name="subconscious", daemon=True)

    def start(self) -> Subconscious:
        # A resumed session already has history. Count it as seen, so the
        # first look waits for something new instead of commenting on the
        # last exchange before the user has said anything.
        messages = self._snapshot()
        if messages:
            self._seen = _mark(messages)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self._nudge.set()
        if self._thread.is_alive():
            self._thread.join(timeout=5)

    def watch(self, provider: Provider) -> Provider:
        """Wrap Nod's provider so its replies reach us before they're persisted."""

        return _Watched(provider, self)

    def wait_at_gate(self, agent) -> None:
        """Real time with patience: Nod waits up to `grace` seconds per step.

        Hooks `_deliver_events`, which runs right before each provider call.
        If the subconscious hasn't yet looked at where Nod stands, Nod waits
        for it, up to `grace` seconds, so a thought that is nearly ready
        lands in this step instead of going stale. If an older look is still
        running, a second one starts on the current state rather than Nod
        waiting behind it. Use with `start` + `watch`.
        """

        deliver = agent._deliver_events

        def wait_then_deliver(session_id: str, messages: list[Message]) -> None:
            # Only wait for a look at exactly where Nod stands. `messages` is
            # the truth here; its last entry has an id unless it's the user's
            # text at the start of a turn, which no look has seen yet.
            here = messages[-1].id if messages else None
            if session_id == self.session_id and self.grace > 0 and here is not None:
                began = time.monotonic()
                deadline = began + self.grace
                outcome = "timed out"
                while time.monotonic() < deadline and not self._stop.is_set():
                    with self._lock:
                        seen, looking = self._seen, here in self._active.values()
                        full = len(self._active) >= MAX_LOOKS
                    if seen == here and not looking:
                        outcome = "caught up"
                        break
                    if seen != here:
                        if full:
                            # Every slot is on an older state; nothing can
                            # look at this one in time.
                            outcome = "behind"
                            break
                        # A fresh look at this state, alongside any older
                        # one still running (which will come back stale).
                        self._begin(extra=True)
                    time.sleep(0.05)
                self.gate_waits.append((time.monotonic() - began, outcome))
                if os.environ.get("SUB_DEBUG"):
                    _say(f"[gate at {len(messages)} msgs: {self.gate_waits[-1][0]:.1f}s, {outcome}]")
            deliver(session_id, messages)

        agent._deliver_events = wait_then_deliver

    def _calling(self) -> None:
        with self._live_lock:
            self._inflight += 1

    def _heard(self, reply: Message | None) -> None:
        with self._live_lock:
            # Only replies that call tools wait to be persisted; the rest
            # (final answers, compaction summaries, dropped empty replies)
            # are written at once or never.
            if reply is not None and reply.tool_calls:
                self._live.append((reply, time.monotonic()))
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
        now = time.monotonic()
        with self._live_lock:
            # The memory sets a message's id when it persists it.
            self._live = [(m, t) for m, t in self._live if m.id is None]
            live = [m for m, t in self._live if now - t >= self.live_delay]
        return messages + live

    def settled(self) -> bool:
        """True once it has looked at the transcript as it stands now."""

        with self._lock:
            if self._active:
                return False
            messages = self._snapshot()
            return not messages or _mark(messages) == self._seen

    def wait_settled(self, timeout: float = 120) -> None:
        deadline = time.monotonic() + timeout
        while not self.settled() and time.monotonic() < deadline:
            time.sleep(0.2)

    # -- the watching loop -------------------------------------------------

    def _loop(self) -> None:
        while True:
            self._nudge.wait(self.poll_seconds)
            self._nudge.clear()
            if self._stop.is_set():
                return
            try:
                self._begin()
            except Exception as exc:  # noqa: BLE001 - never take Nod down with us
                _say(f"[subconscious error] {exc}")

    def _begin(self, extra: bool = False) -> bool:
        """Start a look at the transcript as it stands, if it has moved on.

        One look at a time, except that the gate may ask for an `extra` one
        on the current state while an older look is still running.
        """

        with self._lock:
            if len(self._active) >= (MAX_LOOKS if extra else 1):
                return False
            finished = self._finished
            messages = self._snapshot()
            if not messages or _mark(messages) == self._seen:
                return False
            mark = self._seen = _mark(messages)
            if not self._worth_a_look(messages):
                return False
            carried, self._carry = self._carry, None
            window = self._window(messages)
            self._gen += 1
            gen = self._gen
            self._active[gen] = mark
        threading.Thread(
            target=self._run_look,
            args=(gen, mark, finished, window, carried),
            name=f"subconscious-look-{gen}",
            daemon=True,
        ).start()
        return True

    def _run_look(self, gen, mark, finished, window, carried) -> None:
        try:
            self._look(gen, mark, finished, window, carried)
        except Exception as exc:  # noqa: BLE001 - never take Nod down with us
            _say(f"[subconscious error] {exc}")
        finally:
            with self._lock:
                del self._active[gen]
                if self._carry is not None and not self._active:
                    # Nothing in flight will pick the carried thought up, so
                    # look again even though the transcript hasn't moved.
                    self._seen = None
            self._nudge.set()

    def _look(self, gen, mark, finished, window, carried) -> None:
        recheck = carried is not None

        def moved() -> bool:
            return (
                gen != self._gen  # a newer look has started
                or self._finished != finished
                or _mark(self._snapshot()) != mark
            )

        # A first look that Nod has already overtaken skips the evidence
        # check: it's going to be rechecked, and that look gets checked.
        kind, text, quote, verdict, reply, check = self._ask(
            window, carried, skip_check=lambda: not recheck and moved()
        )
        stale = late = False
        if kind is not None and verdict is None:
            stale = True
        elif verdict == "holds" and recheck:
            # Delivered whatever Nod has done meanwhile; `late` records that
            # it had moved.
            late = moved()
        elif verdict == "holds":
            self._wait_for_nod()
            stale = moved()
        with self._lock:
            if verdict == "holds" and not stale and self.inbox.pending(self.session_id):
                # Two looks can overlap; the other one got its thought in
                # first, and Nod gets one thought at a time.
                verdict = "another thought is already waiting"
            self._log(mark, reply, kind, text, stale, carried, quote, verdict, check, late)
            if kind is None:
                pass
            elif stale:
                # Nod moved on while this formed and may already be there.
                # The next look sees what it did, with this thought carried
                # along to be kept or let go.
                if self._carry is None:
                    self._carry = (kind, text)
                _say(f"~ (stale, rechecking) {kind}: {text}")
            elif verdict != "holds":
                # Unsupported: not posted, and not carried either.
                _say(f"~ (unsupported: {verdict}) {kind}: {text}")
            else:
                self.inbox.post(
                    self.session_id, FRAMES[kind].format(text=text), wake=False
                )
                _say(f"~ {'(late) ' if late else ''}{kind}: {text}")

    def _think(
        self, messages: list[Message]
    ) -> tuple[str | None, str, str | None, str | None, Message, Message | None]:
        """One look at `messages`: (kind, text, quote, verdict, reply, check reply)."""

        return self._ask(self._window(messages))

    def _ask(
        self,
        window: str,
        carried: tuple[str, str] | None = None,
        skip_check=lambda: False,
    ) -> tuple[str | None, str, str | None, str | None, Message, Message | None]:
        """Ask for a thought about `window`, then check its evidence.

        The verdict is None when there is no thought, or when `skip_check`
        said not to bother checking this one.
        """

        transcript = window
        if carried is not None:
            transcript += CARRIED.format(kind=carried[0], text=carried[1])
        reply = self.provider.complete([system(PROMPT), user(transcript)])
        kind, text = parse(reply.text)
        quote = evidence(reply.text)
        verdict, check = None, None
        if kind is not None and not skip_check():
            # Checked against the window only, so a carried thought can't
            # cite itself.
            verdict, check = self.check(kind, text, quote, window)
        return kind, text, quote, verdict, reply, check

    # -- lockstep mode -------------------------------------------------------

    def attach(self, agent) -> None:
        """Lockstep: look before each of Nod's provider calls, Nod waiting.

        Hooks the agent's `_deliver_events`, which runs right before every
        provider call (and once at the start of `send`, before the user's
        text goes in), so a thought posted by the look is delivered in that
        same step. Nothing can go stale: the look sees exactly where Nod is,
        last reply and tool results included. Use this instead of `start`
        and `watch`.
        """

        deliver = agent._deliver_events

        def look_then_deliver(session_id: str, messages: list[Message]) -> None:
            if session_id == self.session_id:
                try:
                    self.look_now(messages)
                except Exception as exc:  # noqa: BLE001 - never take Nod down
                    _say(f"[subconscious error] {exc}")
            deliver(session_id, messages)

        agent._deliver_events = look_then_deliver
        self.mode = "sync"
        # A resumed session's history counts as seen, as in `start`.
        existing = self.memory.load(self.session_id)
        if existing:
            self._seen = _mark(existing)

    def look_now(self, messages: list[Message]) -> str | None:
        """Look at `messages` now and post a thought if one holds up.

        Skips when nothing is new, and when the newest message is the user's
        own: at the start of a turn the look before the user's text goes in
        (Nod's last answer, between messages) is the one that counts.
        """

        if not messages or _mark(messages) == self._seen:
            return None
        last = messages[-1]
        if last.role == "user" and last.kind == "message":
            return None
        mark = self._seen = _mark(messages)
        if not self._worth_a_look(messages):
            return None
        kind, text, quote, verdict, reply, check = self._think(messages)
        self._log(mark, reply, kind, text, False, None, quote, verdict, check)
        if kind is None:
            return None
        if verdict != "holds":
            _say(f"~ (unsupported: {verdict}) {kind}: {text}")
            return None
        thought = FRAMES[kind].format(text=text)
        self.inbox.post(self.session_id, thought, wake=False)
        _say(f"~ {kind}: {text}")
        return thought

    def check(
        self, kind: str, text: str, quote: str | None, transcript: str
    ) -> tuple[str, Message | None]:
        """Whether a thought's evidence holds: "holds", or why not.

        The quote must really be in the transcript (whitespace aside), and a
        second look at it in context must not find a misreading.
        """

        if not quote:
            return "no evidence quoted", None
        if len(" ".join(quote.replace("…", " ").replace("...", " ").split())) < QUOTE_MIN:
            return "quote too short to show anything", None
        found = locate(quote, transcript)
        if found is None:
            return "quote not in the transcript", None
        flat, start, end = found
        context = flat[max(0, start - CONTEXT_BEFORE) : end + CONTEXT_AFTER]
        reply = self.checker.complete(
            [user(CHECK.format(kind=kind, text=text, quote=quote, context=context))]
        )
        line = next((s.strip() for s in reply.text.splitlines() if s.strip()), "")
        if line.lower().startswith("holds"):
            return "holds", reply
        return line or "no verdict from the check", reply

    def _window(self, messages: list[Message]) -> str:
        """The transcript from a start that only moves when it has to."""

        ids = [m.id for m in messages]
        # Live replies have no id yet, so None must never match one.
        known = self._window_from is not None and self._window_from in ids
        start = ids.index(self._window_from) if known else 0
        blocks = render_blocks(messages[start:])
        size = sum(len(b) + 2 for b in blocks)
        if size > WINDOW_CHARS:
            while size > WINDOW_CHARS // 2 and len(blocks) > 1:
                size -= len(blocks.pop(0)) + 2
                start += 1
            # Messages from the file all have ids; only a live reply at the
            # end can lack one, and then the next look starts over from 0.
            self._window_from = messages[start].id
        text = "\n\n".join(b for b in blocks if b)
        return f"{CUT}\n\n{text}" if start else text

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
        quote: str | None,
        verdict: str | None,
        check: Message | None,
        late: bool = False,
    ) -> None:
        record = {
            "at": at,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "mode": self.mode,
            "kind": kind,
            "text": text,
            "evidence": quote,
            # "holds", or why the thought wasn't posted; None when there was
            # no thought or it went stale before being checked.
            "verdict": verdict,
            "stale": stale,
            # Posted after a recheck although Nod had moved again.
            "late": late,
            "carried": f"{carried[0]}: {carried[1]}" if carried else None,
            "raw": reply.text,
            "usage": reply.meta.get("usage"),
            "check_usage": check.meta.get("usage") if check else None,
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


def evidence(raw: str) -> str | None:
    """The quote from an `evidence:` line, without wrapping quote marks."""

    for line in raw.splitlines():
        head, _, rest = line.strip().strip("`").partition(":")
        if head.strip().lower() == "evidence" and rest.strip():
            return rest.strip().strip("`\"'“”‘’").strip() or None
    return None


def locate(quote: str, transcript: str) -> tuple[str, int, int] | None:
    """Find the quote in the transcript with whitespace collapsed.

    Returns the collapsed transcript and the quote's span in it. A quote that
    elides with "..." must have every piece present, in order.
    """

    flat = " ".join(transcript.split())
    pieces = [" ".join(p.split()) for p in re.split(r"\.\.\.|…", quote)]
    pieces = [p for p in pieces if p]
    if not pieces:
        return None
    start = end = flat.find(pieces[0])
    for piece in pieces:
        at = flat.find(piece, end)
        if at < 0:
            return None
        end = at + len(piece)
    return flat, start, end


CUT = "[...earlier transcript cut...]"


def render(messages: list[Message]) -> str:
    """The whole transcript as the subconscious reads it."""

    return "\n\n".join(b for b in render_blocks(messages) if b)


def render_blocks(messages: list[Message]) -> list[str]:
    """Exactly one block per message, "" for one that shows nothing."""

    return [_render_one(m) for m in messages]


def _render_one(m: Message) -> str:
    if m.kind == "compaction_summary":
        return f"EARLIER WORK (summary):\n{m.content}"
    if is_thought(m):
        return f"THOUGHT (yours): {m.content.strip('*')}"
    if m.kind == "event":
        return f"EVENT: {m.content}"
    if m.role == "user":
        return f"USER: {m.content}"
    if m.role == "tool":
        return f"RESULT: {_clip(m.content, RESULT_CHARS)}"
    parts: list[str] = []
    if m.reasoning:
        parts.append(f"NOD (thinking): {_clip(m.reasoning, REASONING_CHARS)}")
    if m.content:
        parts.append(f"NOD: {m.content}")
    for call in m.tool_calls:
        args = json.dumps(call.arguments, ensure_ascii=False)
        parts.append(f"NOD calls {call.name}({_clip(args, RESULT_CHARS)})")
    return "\n\n".join(parts)


def _clip(s: str, n: int) -> str:
    if len(s) <= n:
        return s
    half = n // 2
    return f"{s[:half]}\n[...{len(s) - n} chars cut...]\n{s[-half:]}"


def _say(line: str) -> None:
    # Dim magenta on stderr, so it's visible but apart from Nod's own output.
    sys.stderr.write(f"\033[2;35m{line}\033[0m\n")
    sys.stderr.flush()
