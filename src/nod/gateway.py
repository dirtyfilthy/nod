"""Gateways: how the outside world talks to the agent.

A `Gateway` takes a wired `Agent` and runs an I/O loop against it. The only one
shipped here is the CLI. An async gateway (Telegram, email) would run the sync
agent in a worker thread; the agent itself never needs to know.

A gateway has two jobs: pass user input to `agent.send`, and call
`agent.wake` when something is posted to the inbox while the agent is idle,
so a finished background job gets a turn without the user typing anything.
"""

from __future__ import annotations

import queue
import sys
import threading
from typing import Protocol

from nod.agent import Agent
from nod.provider import OnToken


class Gateway(Protocol):
    def run(self, agent: Agent) -> None: ...


class CLIGateway:
    """A read-eval-print loop over stdin/stdout with live token streaming.

    `input()` blocks and can't be interrupted by an inbox post, so a reader
    thread feeds user lines into a queue that inbox wake-ups also go into, and
    the main loop handles whichever arrives first. A wake-up's reply prints
    like a normal one, which means it can land over a half-typed line.
    """

    def __init__(self, session_id: str = "cli", prompt: str = "you> ") -> None:
        self.session_id = session_id
        self.prompt = prompt

    def run(self, agent: Agent) -> None:
        print("Nod is listening. Ctrl-D or 'exit' to leave.\n")
        events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        ready = threading.Event()

        def read_lines() -> None:
            while True:
                ready.wait()
                ready.clear()
                try:
                    line = input(self.prompt)
                except (EOFError, KeyboardInterrupt):
                    events.put(("eof", None))
                    return
                events.put(("line", line))

        agent.inbox.subscribe(lambda session: events.put(("wake", session)))
        # Anything already waiting (jobs a previous run lost) gets a turn now.
        if agent.inbox.pending(self.session_id):
            events.put(("wake", self.session_id))

        threading.Thread(target=read_lines, name="nod-cli-reader", daemon=True).start()
        ready.set()
        while True:
            try:
                kind, payload = events.get()
            except KeyboardInterrupt:
                print()
                break
            if kind == "eof":
                print()
                break
            if kind == "wake":
                self._wake(agent, payload or self.session_id)
                continue
            text = (payload or "").strip()
            if text in {"exit", "quit"}:
                break
            if text:
                self._send(agent, text)
            ready.set()

    def _send(self, agent: Agent, text: str) -> None:
        sys.stdout.write("nod> ")
        sys.stdout.flush()
        on_token, streamed = self._streamer()
        try:
            final = agent.send(text, session_id=self.session_id, on_token=on_token)
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            print(f"\n[error] {exc}\n")
            return
        self._finish(final, streamed)

    def _wake(self, agent: Agent, session_id: str) -> None:
        # Only this thread runs turns, so a non-empty inbox here means the
        # wake-up will either run a turn or hit the cap.
        if not agent.inbox.pending(session_id):
            return
        sys.stdout.write("\nnod> ")
        sys.stdout.flush()
        on_token, streamed = self._streamer()
        try:
            final = agent.wake(session_id, on_token=on_token)
        except Exception as exc:  # noqa: BLE001 - keep the REPL alive
            print(f"\n[error] {exc}\n")
            return
        if final is None:
            print("(a job finished; the wake-up cap was hit, so it waits for your next message)\n")
            return
        self._finish(final, streamed)

    @staticmethod
    def _streamer() -> tuple[OnToken, list[bool]]:
        streamed = [False]

        def on_token(tok: str) -> None:
            streamed[0] = True
            sys.stdout.write(tok)
            sys.stdout.flush()

        return on_token, streamed

    @staticmethod
    def _finish(final: str, streamed: list[bool]) -> None:
        # If nothing streamed (e.g. the turn was all tool calls), print the
        # final text so the user still sees an answer.
        if not streamed[0] and final:
            sys.stdout.write(final)
        sys.stdout.write("\n\n")
        sys.stdout.flush()
