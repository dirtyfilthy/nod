"""Gateways: how the outside world talks to the agent.

A `Gateway` takes a wired `Agent` and runs an I/O loop against it. The only one
shipped here is the CLI. An async gateway (Telegram, email) would run the sync
agent in a worker thread; the agent itself never needs to know.
"""

from __future__ import annotations

import sys
from typing import Protocol

from nod.agent import Agent


class Gateway(Protocol):
    def run(self, agent: Agent) -> None: ...


class CLIGateway:
    """A read-eval-print loop over stdin/stdout with live token streaming."""

    def __init__(self, session_id: str = "cli", prompt: str = "you> ") -> None:
        self.session_id = session_id
        self.prompt = prompt

    def run(self, agent: Agent) -> None:
        print("Nod is listening. Ctrl-D or 'exit' to leave.\n")
        while True:
            try:
                line = input(self.prompt)
            except (EOFError, KeyboardInterrupt):
                print()
                break
            text = line.strip()
            if not text:
                continue
            if text in {"exit", "quit"}:
                break

            sys.stdout.write("nod> ")
            sys.stdout.flush()
            streamed = False

            def on_token(tok: str) -> None:
                nonlocal streamed
                streamed = True
                sys.stdout.write(tok)
                sys.stdout.flush()

            try:
                final = agent.send(
                    text, session_id=self.session_id, on_token=on_token
                )
            except Exception as exc:  # noqa: BLE001 - keep the REPL alive
                print(f"\n[error] {exc}\n")
                continue

            # If nothing streamed (e.g. the turn was all tool calls), print the
            # final text so the user still sees an answer.
            if not streamed and final:
                sys.stdout.write(final)
            sys.stdout.write("\n\n")
            sys.stdout.flush()
