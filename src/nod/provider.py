"""The rented brain.

`Provider` is the one interface the agent loop talks to. Anything that can turn a
list of messages (plus the tool schemas it may call) into a single assistant reply
qualifies. The default speaks the OpenAI-compatible streaming API, which covers
OpenRouter and most local servers.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Protocol

import httpx

from nod.messages import Message, ToolCall

OnToken = Callable[[str], None]


class Provider(Protocol):
    """Turns a conversation into the next assistant message."""

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        on_token: OnToken | None = None,
    ) -> Message: ...


class ProviderError(RuntimeError):
    """Raised when the upstream endpoint refuses or fails a request."""


class OpenAICompatibleProvider:
    """Default brain: any OpenAI-compatible chat-completions endpoint.

    Streams the response so tokens can be surfaced live via `on_token`, and
    reassembles streamed tool-call deltas into whole `ToolCall`s.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str | None = None,
        api_key_env: str = "OPENROUTER_API_KEY",
        timeout: float = 120.0,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.timeout = timeout
        self.extra_headers = extra_headers or {}
        if not self.api_key:
            raise ProviderError(
                f"No API key. Set ${api_key_env} or pass api_key explicitly."
            )

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        on_token: OnToken | None = None,
    ) -> Message:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_provider_dict() for m in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        content_parts: list[str] = []
        tool_acc = _ToolCallAccumulator()

        try:
            with httpx.Client(timeout=self.timeout) as client:
                with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                ) as response:
                    if response.status_code >= 400:
                        body = response.read().decode("utf-8", "replace")
                        raise ProviderError(
                            f"{response.status_code} from provider: {body}"
                        )
                    for line in response.iter_lines():
                        delta = _parse_sse_line(line)
                        if delta is None:
                            continue
                        text = delta.get("content")
                        if text:
                            content_parts.append(text)
                            if on_token is not None:
                                on_token(text)
                        for tc in delta.get("tool_calls", []) or []:
                            tool_acc.add(tc)
        except httpx.HTTPError as exc:
            raise ProviderError(f"request to provider failed: {exc}") from exc

        return Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_acc.finish(),
        )


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Extract the choice delta from one server-sent-events line, if any."""

    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = chunk.get("choices") or []
    if not choices:
        return None
    return choices[0].get("delta") or {}


class _ToolCallAccumulator:
    """Reassembles streamed tool-call fragments keyed by their stream index."""

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, str]] = {}
        self._order: list[int] = []

    def add(self, fragment: dict[str, Any]) -> None:
        index = fragment.get("index", 0)
        slot = self._by_index.get(index)
        if slot is None:
            slot = {"id": "", "name": "", "arguments": ""}
            self._by_index[index] = slot
            self._order.append(index)
        if fragment.get("id"):
            slot["id"] = fragment["id"]
        fn = fragment.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]

    def finish(self) -> list[ToolCall]:
        calls: list[ToolCall] = []
        for index in self._order:
            slot = self._by_index[index]
            if not slot["name"]:
                continue
            calls.append(
                ToolCall.from_provider_dict(
                    {
                        "id": slot["id"] or f"call_{index}",
                        "function": {
                            "name": slot["name"],
                            "arguments": slot["arguments"] or "{}",
                        },
                    }
                )
            )
        return calls
