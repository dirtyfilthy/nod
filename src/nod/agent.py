"""The agent: a thin synchronous loop.

Read it top to bottom -- that's the whole point. Ask the brain for a reply; if it
asked to run tools, run them and ask again; otherwise we're done. History is loaded
and saved through the `Memory` interface, and the system prompt is just the persona
(`SOUL.md`) plus the skills index.
"""

from __future__ import annotations

from nod.compaction import CompactionConfig, CompactionResult, compact_if_needed
from nod.memory import Memory
from nod.messages import Message, system, tool_result, user
from nod.provider import OnToken, Provider
from nod.skills import SkillLoader
from nod.tools import ToolRegistry

DEFAULT_SESSION = "default"


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
    ) -> None:
        self.provider = provider
        self.registry = registry or ToolRegistry()
        self.skills = skills
        self.soul = soul.strip()
        self.max_turns = max_turns
        self.compaction = compaction

        if memory is None:
            from nod.memory import InMemoryMemory

            memory = InMemoryMemory()
        self.memory = memory

        # Skills are progressive-disclosure: only the read_skill tool lives in the
        # registry; the catalogue goes into the system prompt.
        if self.skills is not None and self.skills.skills:
            self.registry.register(self.skills.read_skill_tool())

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

    def send(
        self,
        user_text: str,
        *,
        session_id: str = DEFAULT_SESSION,
        on_token: OnToken | None = None,
    ) -> str:
        messages = self.memory.load(session_id)
        messages = self._compact(session_id, messages)
        prompt = self.system_prompt()
        sys_msg = system(prompt) if prompt else None
        messages.append(user(user_text))

        reply = Message(role="assistant")
        for _ in range(self.max_turns):
            provider_messages = ([sys_msg] if sys_msg is not None else []) + messages
            reply = self.provider.complete(
                provider_messages, self.registry.schemas() or None, on_token
            )
            messages.append(reply)
            if not reply.tool_calls:
                break
            for call in reply.tool_calls:
                result = self.registry.dispatch(call)
                messages.append(tool_result(call.id, result))
            self.memory.persist_messages(session_id, messages)
            messages = self._compact(session_id, messages)
        else:
            # Loop bound hit while still calling tools. Record the ceiling so the
            # transcript explains the abrupt stop.
            note = Message(
                role="assistant",
                content="(stopped: hit the max_turns limit before finishing)",
            )
            messages.append(note)
            reply = note

        self.memory.persist_messages(session_id, messages)
        return reply.text
