"""Conversation primitives.

A conversation is just a list of `Message` objects. These map directly onto the
OpenAI-compatible chat format that providers expect, but the dataclasses keep the
rest of the code from caring about wire details.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]

    def to_provider_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments),
            },
        }

    @classmethod
    def from_provider_dict(cls, raw: dict[str, Any]) -> ToolCall:
        fn = raw.get("function", {})
        raw_args = fn.get("arguments") or "{}"
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            # Models occasionally emit malformed JSON. Hand it on verbatim so the
            # tool can decide what to do rather than crashing the loop.
            arguments = {"_raw": raw_args}
        return cls(id=raw.get("id", ""), name=fn.get("name", ""), arguments=arguments)


@dataclass
class Message:
    """One entry in the conversation.

    `role` is one of: system, user, assistant, tool. Only the fields relevant to a
    given role are populated; the serialization drops the rest.

    `reasoning` is a thinking model's chain of thought for an assistant reply, and
    `reasoning_details` the provider's structured form of it (OpenRouter's
    `reasoning_details`), kept exactly as received. Both are stored and sent back:
    in a tool loop, thinking models such as DeepSeek's expect their earlier
    reasoning in later requests so they can continue it.
    """

    role: str
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    id: str | None = None
    kind: str = "message"
    reasoning: str = ""
    reasoning_details: list[dict[str, Any]] = field(default_factory=list)

    @property
    def text(self) -> str:
        return self.content or ""

    def to_provider_dict(self) -> dict[str, Any]:
        role = "user" if self.kind == "compaction_summary" else self.role
        out: dict[str, Any] = {"role": role, "content": self.content}
        if self.tool_calls:
            out["tool_calls"] = [tc.to_provider_dict() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            out["tool_call_id"] = self.tool_call_id
        if self.role == "assistant":
            # Structured details go back verbatim when the provider sent them
            # (OpenRouter requires them unmodified). Otherwise the plain text goes
            # back as `reasoning_content`, which DeepSeek's API and vLLM read and
            # OpenRouter treats as an alias of `reasoning`.
            if self.reasoning_details:
                out["reasoning_details"] = self.reasoning_details
            elif self.reasoning:
                out["reasoning_content"] = self.reasoning
        return out

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                for tc in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "reasoning": self.reasoning,
            "reasoning_details": self.reasoning_details,
        }

    @classmethod
    def from_storage_dict(cls, raw: dict[str, Any]) -> Message:
        return cls(
            role=raw["role"],
            content=raw.get("content", "") or "",
            tool_calls=[
                ToolCall(id=tc["id"], name=tc["name"], arguments=tc["arguments"])
                for tc in raw.get("tool_calls", [])
            ],
            tool_call_id=raw.get("tool_call_id"),
            id=raw.get("id"),
            kind=raw.get("kind", "message"),
            reasoning=raw.get("reasoning", "") or "",
            reasoning_details=raw.get("reasoning_details") or [],
        )


def system(content: str) -> Message:
    return Message(role="system", content=content)


def user(content: str) -> Message:
    return Message(role="user", content=content)


def assistant(content: str = "", tool_calls: list[ToolCall] | None = None) -> Message:
    return Message(role="assistant", content=content, tool_calls=tool_calls or [])


def tool_result(tool_call_id: str, content: str) -> Message:
    return Message(role="tool", content=content, tool_call_id=tool_call_id)
