"""The agent: a thin synchronous loop.

Read it top to bottom -- that's the whole point. Ask the brain for a reply; if it
asked to run tools, run them and ask again; otherwise we're done. History is loaded
and saved through the `Memory` interface, and the system prompt is just the persona
(`SOUL.md`) plus the skills index.

Two things happen around that loop. Before every provider call the agent empties
the session's `Inbox` (events posted by background jobs or anything else that
runs off the loop) into the conversation as user messages. And a turn can start
without new user text: `wake` runs one when the inbox has something pending,
which is how an idle agent hears that a job finished.
"""

from __future__ import annotations

import threading
from contextvars import ContextVar

from nod.compaction import CompactionConfig, compact_if_needed
from nod.inbox import Inbox, InMemoryInbox
from nod.memory import Memory
from nod.messages import Message, system, tool_result, user
from nod.provider import OnToken, Provider
from nod.skills import SkillLoader
from nod.tools import ToolRegistry

DEFAULT_SESSION = "default"
# How many wake-ups may run back to back without the user saying anything. A
# job can start another job whose event wakes the agent again; without a cap
# that cycle spends forever.
DEFAULT_MAX_WAKEUPS = 5
# Extra provider calls when a reply comes back with no text and no tool calls.
# Such a reply would otherwise end the turn as if the model had finished; in
# practice it is a provider returning nothing or a model that reasoned and then
# said nothing, and asking again usually gets a real answer.
EMPTY_REPLY_RETRIES = 2

# The session whose turn is running on this thread. Tools that need to know
# which session they belong to (the job tools) read it through
# `current_session`; a child agent running in its own thread sets its own.
_current_session: ContextVar[str] = ContextVar("nod_session", default=DEFAULT_SESSION)


def current_session() -> str:
    return _current_session.get()


class Agent:
    def __init__(
        self,
        *,
        provider: Provider,
        registry: ToolRegistry | None = None,
        memory: Memory | None = None,
        skills: SkillLoader | None = None,
        soul: str = "",
        max_turns: int = 20,
        compaction: CompactionConfig | None = None,
        inbox: Inbox | None = None,
        max_wakeups: int = DEFAULT_MAX_WAKEUPS,
    ) -> None:
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.skills = skills
        self.soul = soul.strip()
        self.max_turns = max_turns
        self.compaction = compaction
        self.inbox: Inbox = inbox if inbox is not None else InMemoryInbox()
        self.max_wakeups = max_wakeups

        if memory is None:
            from nod.memory import InMemoryMemory

            memory = InMemoryMemory()
        self.memory = memory

        # One lock per session: `send` and `wake` take it, so a wake-up never
        # runs over a user's turn (or another wake-up) on the same session.
        self._locks_guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._wakeups_in_a_row: dict[str, int] = {}

        # Skills are progressive-disclosure: only the read_skill tool lives in the
        # registry; the catalogue goes into the system prompt.
        if self.skills is not None and self.skills.skills:
            self.registry.register(self.skills.read_skill_tool())

    def session_lock(self, session_id: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[session_id] = lock
            return lock

    def system_prompt(self) -> str:
        parts = [self.soul] if self.soul else []
        if self.skills is not None:
            index = self.skills.index_text()
            if index:
                parts.append(index)
        return "\n\n".join(parts)

    def _compact(self, session_id: str, messages: list[Message]) -> list[Message]:
        result = compact_if_needed(messages, self.provider, self.compaction)
        if not result.compacted:
            return result.messages
        if result.first_kept_entry_id is None or result.summary is None:
            return result.messages
        compaction_id = self.memory.persist_compaction(
            session_id,
            result.summary,
            result.first_kept_entry_id,
            result.tokens_before,
        )
        result.messages[0].id = compaction_id
        return result.messages

    def _deliver_events(self, session_id: str, messages: list[Message]) -> None:
        """Move pending inbox events into the conversation.

        Persist first, acknowledge after: if the persist fails the events stay
        in the inbox for the next attempt, and a provider call that raises
        afterwards finds them already in the transcript. The persist also
        writes anything else in `messages` that hasn't been written yet, such
        as the user text a failing first provider call would otherwise lose.
        """
        events = self.inbox.pending(session_id)
        for event in events:
            messages.append(Message(role="user", content=event.text, kind="event"))
        self.memory.persist_messages(session_id, messages)
        self.inbox.ack(session_id, [e.id for e in events])

    def send(
        self,
        user_text: str,
        *,
        session_id: str = DEFAULT_SESSION,
        on_token: OnToken | None = None,
    ) -> str:
        with self.session_lock(session_id):
            self._wakeups_in_a_row[session_id] = 0
            messages = self.memory.load(session_id)
            messages = self._compact(session_id, messages)
            # Events that arrived while the agent was idle happened before the
            # user spoke, so they go in first.
            self._deliver_events(session_id, messages)
            messages.append(user(user_text))
            return self._run_turn(session_id, messages, on_token)

    def wake(
        self,
        session_id: str = DEFAULT_SESSION,
        *,
        on_token: OnToken | None = None,
    ) -> str | None:
        """Run a turn with the pending inbox events as its only input.

        Returns the reply, or None when nothing was pending or the run of
        consecutive wake-ups has hit `max_wakeups`. In the second case the
        events stay in the inbox and are delivered at the start of the next
        `send`, which also resets the count.
        """
        with self.session_lock(session_id):
            if not self.inbox.pending(session_id):
                return None
            if self._wakeups_in_a_row.get(session_id, 0) >= self.max_wakeups:
                return None
            self._wakeups_in_a_row[session_id] = (
                self._wakeups_in_a_row.get(session_id, 0) + 1
            )
            messages = self.memory.load(session_id)
            messages = self._compact(session_id, messages)
            return self._run_turn(session_id, messages, on_token)

    def _complete(
        self, provider_messages: list[Message], on_token: OnToken | None
    ) -> Message:
        """One provider call, asked again up to EMPTY_REPLY_RETRIES times while
        the reply is empty.

        Empty replies are dropped rather than added to the conversation, but
        their meta (usage and cost included) goes on the kept reply under
        `empty_replies` so the transcript still accounts for them. If every
        attempt is empty, the last one is returned and ends the turn as before.
        """
        dropped: list[dict] = []
        while True:
            reply = self.provider.complete(
                provider_messages, self.registry.schemas() or None, on_token
            )
            if reply.tool_calls or reply.text.strip():
                break
            if len(dropped) == EMPTY_REPLY_RETRIES:
                break
            dropped.append(reply.meta)
        if dropped:
            reply.meta["empty_replies"] = dropped
        return reply

    def _run_turn(
        self,
        session_id: str,
        messages: list[Message],
        on_token: OnToken | None,
    ) -> str:
        prompt = self.system_prompt()
        sys_msg = system(prompt) if prompt else None
        token = _current_session.set(session_id)
        try:
            reply = Message(role="assistant")
            for _ in range(self.max_turns):
                self._deliver_events(session_id, messages)
                provider_messages = ([sys_msg] if sys_msg is not None else []) + messages
                reply = self._complete(provider_messages, on_token)
                messages.append(reply)
                if not reply.tool_calls:
                    break
                for call in reply.tool_calls:
                    result = self.registry.dispatch(call)
                    messages.append(tool_result(call.id, result))
                self.memory.persist_messages(session_id, messages)
                messages = self._compact(session_id, messages)
            else:
                # Loop bound hit while still calling tools. Record the ceiling so
                # the transcript explains the abrupt stop.
                note = Message(
                    role="assistant",
                    content="(stopped: hit the max_turns limit before finishing)",
                )
                messages.append(note)
                reply = note

            self.memory.persist_messages(session_id, messages)
            return reply.text
        finally:
            _current_session.reset(token)
