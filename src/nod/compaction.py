"""Context compaction for long conversations.

Compaction replaces old working context with a structured checkpoint summary
while append-only memory keeps the full raw history.
"""

from __future__ import annotations

from dataclasses import dataclass
import json

from nod.messages import Message, system, user
from nod.provider import Provider

SUMMARY_TOOL_RESULT_LIMIT = 2000


@dataclass(frozen=True)
class CompactionConfig:
    enabled: bool = True
    context_window: int = 512_000
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000


@dataclass(frozen=True)
class CompactionResult:
    messages: list[Message]
    summary: str | None = None
    first_kept_entry_id: str | None = None
    tokens_before: int = 0

    @property
    def compacted(self) -> bool:
        return self.summary is not None


SUMMARIZATION_SYSTEM_PROMPT = (
    "You compress agent conversation history into a checkpoint for another LLM. "
    "Preserve exact file paths, commands, function names, error messages, and "
    "decisions needed to continue the work. Do not continue the conversation."
)

SUMMARIZATION_PROMPT = """The messages above are a conversation to summarize. Create a structured context checkpoint summary that another LLM will use to continue the work.

Use this EXACT format:

## Goal
[What is the user trying to accomplish? Can be multiple items if the session covers different tasks.]

## Constraints & Preferences
- [Any constraints, preferences, or requirements mentioned by user]
- [Or "(none)" if none were mentioned]

## Progress
### Done
- [x] [Completed tasks/changes]

### In Progress
- [ ] [Current work]

### Blocked
- [Issues preventing progress, if any]

## Key Decisions
- **[Decision]**: [Brief rationale]

## Next Steps
1. [Ordered list of what should happen next]

## Critical Context
- [Any data, examples, or references needed to continue]
- [Or "(none)" if not applicable]

Keep each section concise. Preserve exact file paths, function names, and error messages."""

UPDATE_SUMMARIZATION_PROMPT = """The messages above are NEW conversation messages to incorporate into the existing summary provided in <previous-summary> tags.

Update the existing structured summary with new information. RULES:
- PRESERVE all existing information from the previous summary
- ADD new progress, decisions, and context from the new messages
- UPDATE the Progress section: move items from "In Progress" to "Done" when completed
- UPDATE "Next Steps" based on what was accomplished
- PRESERVE exact file paths, function names, and error messages
- If something is no longer relevant, you may remove it

Use this EXACT format:

## Goal
[Preserve existing goals, add new ones if the task expanded]

## Constraints & Preferences
- [Preserve existing, add new ones discovered]

## Progress
### Done
- [x] [Include previously done items AND newly completed items]

### In Progress
- [ ] [Current work - update based on progress]

### Blocked
- [Current blockers - remove if resolved]

## Key Decisions
- **[Decision]**: [Brief rationale] (preserve all previous, add new)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Preserve important context, add new if needed]

Keep each section concise. Preserve exact file paths, function names, and error messages."""


def estimate_tokens(message: Message) -> int:
    """Rough chars/4 token estimate, good enough for cut-point selection."""
    # Reasoning is sent back with the message, so it occupies context too.
    chars = len(message.content or "") + len(message.reasoning or "")
    if message.tool_calls:
        chars += len(
            json.dumps(
                [
                    {"name": call.name, "arguments": call.arguments}
                    for call in message.tool_calls
                ],
                separators=(",", ":"),
            )
        )
    if message.tool_call_id:
        chars += len(message.tool_call_id)
    return max(1, (chars + 3) // 4)


def total_tokens(messages: list[Message]) -> int:
    return sum(estimate_tokens(message) for message in messages)


def find_cut_point(messages: list[Message], keep_recent_tokens: int) -> int:
    """Return first index to keep, never cutting at a tool result."""
    if not messages:
        return 0

    valid_cut_points = [
        i
        for i, message in enumerate(messages)
        if message.role in {"user", "assistant"} and message.kind != "compaction_summary"
    ]
    if not valid_cut_points:
        return 0

    accumulated = 0
    cut_index = valid_cut_points[0]
    for i in range(len(messages) - 1, -1, -1):
        accumulated += estimate_tokens(messages[i])
        if accumulated < keep_recent_tokens:
            continue
        for candidate in valid_cut_points:
            if candidate >= i:
                cut_index = candidate
                break
        else:
            cut_index = valid_cut_points[-1]
        break
    return cut_index


def serialize_for_summary(messages: list[Message]) -> str:
    chunks: list[str] = []
    for message in messages:
        if message.kind == "compaction_summary":
            continue
        if message.role == "user":
            chunks.append(f"[User]: {message.content}")
        elif message.role == "assistant":
            if message.content:
                chunks.append(f"[Assistant]: {message.content}")
            if message.tool_calls:
                calls = "; ".join(
                    f"{call.name}({json.dumps(call.arguments, separators=(',', ':'))})"
                    for call in message.tool_calls
                )
                chunks.append(f"[Assistant tool calls]: {calls}")
        elif message.role == "tool":
            content = message.content
            if len(content) > SUMMARY_TOOL_RESULT_LIMIT:
                omitted = len(content) - SUMMARY_TOOL_RESULT_LIMIT
                content = (
                    f"{content[:SUMMARY_TOOL_RESULT_LIMIT]}\n"
                    f"[truncated {omitted} characters]"
                )
            chunks.append(f"[Tool result {message.tool_call_id or '?'}]: {content}")
        elif message.role == "system":
            continue
        else:
            chunks.append(f"[{message.role}]: {message.content}")
    return "\n\n".join(chunks)


def summarize(
    provider: Provider,
    messages: list[Message],
    previous_summary: str | None = None,
    custom_instructions: str | None = None,
) -> str:
    conversation = serialize_for_summary(messages)
    prompt = f"<conversation>\n{conversation}\n</conversation>\n\n"
    if previous_summary:
        prompt += f"<previous-summary>\n{previous_summary}\n</previous-summary>\n\n"
        prompt += UPDATE_SUMMARIZATION_PROMPT
    else:
        prompt += SUMMARIZATION_PROMPT
    if custom_instructions:
        prompt += f"\n\nAdditional focus: {custom_instructions}"

    reply = provider.complete(
        [system(SUMMARIZATION_SYSTEM_PROMPT), user(prompt)], tools=None, on_token=None
    )
    return reply.content


def compact_if_needed(
    messages: list[Message],
    provider: Provider,
    cfg: CompactionConfig | None,
) -> CompactionResult:
    if cfg is None or not cfg.enabled:
        return CompactionResult(messages=messages, tokens_before=total_tokens(messages))

    tokens_before = total_tokens(messages)
    if tokens_before <= cfg.context_window - cfg.reserve_tokens:
        return CompactionResult(messages=messages, tokens_before=tokens_before)

    previous_summary: str | None = None
    working = messages
    if working and working[0].kind == "compaction_summary":
        previous_summary = working[0].content
        working = working[1:]

    cut = find_cut_point(working, cfg.keep_recent_tokens)
    to_summarize = working[:cut]
    kept = working[cut:]
    if not to_summarize or not kept or kept[0].id is None:
        return CompactionResult(messages=messages, tokens_before=tokens_before)

    summary_text = summarize(provider, to_summarize, previous_summary)
    summary_message = Message(
        role="user",
        content=summary_text,
        kind="compaction_summary",
    )
    return CompactionResult(
        messages=[summary_message, *kept],
        summary=summary_text,
        first_kept_entry_id=kept[0].id,
        tokens_before=tokens_before,
    )
