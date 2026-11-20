"""Inbox: the channel for things that happen while the agent isn't looking.

Anything asynchronous (a background job finishing, a cron tick, an email)
posts a text event to a session's inbox. The agent empties the inbox before
each provider call and turns each event into a user message, so the model
hears about it at the next opportunity. Events are acknowledged only after
they have been persisted, so a failed provider call never loses one.

The inbox is in memory on purpose: sources that need durability bring their
own queue in front of it and re-post.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Event:
    id: str
    text: str


class Inbox(Protocol):
    def post(self, session_id: str, text: str, *, wake: bool = True) -> str: ...

    def pending(self, session_id: str) -> list[Event]: ...

    def ack(self, session_id: str, event_ids: list[str]) -> None: ...

    def subscribe(self, callback: Callable[[str], None]) -> None: ...


class InMemoryInbox:
    """Per-session event lists guarded by one lock.

    `subscribe` registers a callback that is invoked with the session id after
    every post. Gateways use it to wake an idle agent; the callback runs on the
    poster's thread, so it must return quickly (hand the work to another
    thread or a queue). A post with `wake=False` skips the callbacks: the
    event waits for the session's next turn instead of starting one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: dict[str, list[Event]] = {}
        self._subscribers: list[Callable[[str], None]] = []
        self._next_id = 1

    def post(self, session_id: str, text: str, *, wake: bool = True) -> str:
        with self._lock:
            event = Event(id=f"ev{self._next_id}", text=text)
            self._next_id += 1
            self._events.setdefault(session_id, []).append(event)
            subscribers = list(self._subscribers) if wake else []
        for callback in subscribers:
            callback(session_id)
        return event.id

    def pending(self, session_id: str) -> list[Event]:
        with self._lock:
            return list(self._events.get(session_id, []))

    def ack(self, session_id: str, event_ids: list[str]) -> None:
        if not event_ids:
            return
        drop = set(event_ids)
        with self._lock:
            events = self._events.get(session_id)
            if events:
                events[:] = [e for e in events if e.id not in drop]

    def subscribe(self, callback: Callable[[str], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)
