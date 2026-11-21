"""The rented brain.

`Provider` is the one interface the agent loop talks to. Anything that can turn a
list of messages (plus the tool schemas it may call) into a single assistant reply
qualifies. The default speaks the OpenAI-compatible streaming API, which covers
OpenRouter and most local servers.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Iterable, Protocol

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


# Keys in the routing config that say which models it applies to. They are
# Nod's own and are never sent.
ROUTING_SCOPE_KEYS = ("models", "model_prefix")


def routing_for(model: str, routing: dict[str, Any]) -> dict[str, Any]:
    """The `provider` field to send for `model`, or {} to send none.

    Routing with neither scope key applies to every model. With `models` (exact
    ids) or `model_prefix` it applies only to a model that matches either one.
    """

    models = routing.get("models")
    prefix = routing.get("model_prefix")
    if models is None and prefix is None:
        return routing
    if isinstance(models, str):
        models = [models]
    matches = model in (models or []) or (bool(prefix) and model.startswith(prefix))
    if not matches:
        return {}
    return {k: v for k, v in routing.items() if k not in ROUTING_SCOPE_KEYS}


class OpenAICompatibleProvider:
    """Default brain: any OpenAI-compatible chat-completions endpoint.

    Streams the response so tokens can be surfaced live via `on_token`, and
    reassembles streamed tool-call deltas into whole `ToolCall`s. A thinking
    model's streamed reasoning is collected onto the returned message (never
    passed to `on_token`) so the agent can store it and send it back.
    """

    def __init__(
        self,
        model: str,
        *,
        base_url: str = "https://openrouter.ai/api/v1",
        api_key: str | None = None,
        api_key_env: str = "OPENROUTER_API_KEY",
        timeout: float = 120.0,
        deadline_seconds: float | None = 600.0,
        extra_headers: dict[str, str] | None = None,
        provider_routing: dict[str, Any] | None = None,
        reasoning: dict[str, Any] | None = None,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or os.environ.get(api_key_env, "")
        self.timeout = timeout
        # Overall wall-clock budget for one completion. The httpx `timeout` is
        # per-read, which a trickling-but-stalled SSE stream (e.g. keep-alive
        # comments) can dodge forever; this bounds the whole stream so a wedged
        # request fails instead of hanging the agent loop / a service thread.
        # Set to None to disable.
        self.deadline_seconds = deadline_seconds
        self.extra_headers = extra_headers or {}
        # OpenRouter's `provider` request field (order, allow_fallbacks, ...).
        # Pins which upstream serves the model; omitted from the request when
        # empty, or when its `models` / `model_prefix` don't cover this model.
        self.provider_routing = provider_routing or {}
        # OpenRouter's `reasoning` request field, e.g. {"effort": "high"} or
        # {"enabled": False}. Omitted when empty, leaving the model's default.
        self.reasoning = reasoning or {}
        # Extra attempts for failures that are worth trying again: a stalled
        # stream, a dropped connection, 429 or 5xx. Other errors fail at once.
        self.retries = retries
        self._sleep = sleep
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
        attempt = 0
        while True:
            attempt += 1
            started_at = time.time()
            try:
                reply = self._complete_once(messages, tools, on_token)
            except _RetryableError as exc:
                if attempt > self.retries:
                    raise ProviderError(str(exc)) from exc
                # A retried stream may already have sent some tokens to on_token.
                self._sleep(min(60.0, 5.0 * 2 ** (attempt - 1)))
                continue
            reply.meta["started_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(started_at)
            )
            reply.meta["attempts"] = attempt
            return reply

    def _complete_once(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
        on_token: OnToken | None,
    ) -> Message:
        payload = self._payload(messages, tools)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            **self.extra_headers,
        }

        start = time.monotonic()
        deadline = start + self.deadline_seconds if self.deadline_seconds else None

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
                        error = (
                            _RetryableError
                            if response.status_code == 429 or response.status_code >= 500
                            else ProviderError
                        )
                        raise error(f"{response.status_code} from provider: {body}")
                    return self._consume(
                        response.iter_lines(), on_token, deadline, start=start
                    )
        except httpx.HTTPError as exc:
            raise _RetryableError(f"request to provider failed: {exc}") from exc

    def _payload(
        self, messages: list[Message], tools: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_provider_dict() for m in messages],
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
        routing = routing_for(self.model, self.provider_routing)
        if routing:
            payload["provider"] = routing
        if self.reasoning:
            payload["reasoning"] = self.reasoning
        return payload

    def _consume(
        self,
        lines: Iterable[str],
        on_token: OnToken | None,
        deadline: float | None,
        *,
        start: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> Message:
        """Assemble streamed SSE lines into one message, aborting past `deadline`.

        Also records how the reply was produced in `meta`: time to first token
        and total duration (from `start`), the serving provider, the generation
        id, and token usage and cost from the final chunk. Factored out so the
        streaming/deadline logic is testable without a live connection.
        """

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_acc = _ToolCallAccumulator()
        details_acc = _ReasoningDetailsAccumulator()
        meta: dict[str, Any] = {}
        for line in lines:
            if deadline is not None and now() >= deadline:
                raise _RetryableError(
                    f"provider stream exceeded {self.deadline_seconds:.0f}s "
                    "deadline (stalled?)"
                )
            chunk = _parse_sse_chunk(line)
            if chunk is None:
                continue
            for key, name in (("provider", "provider"), ("id", "generation_id")):
                if chunk.get(key):
                    meta[name] = chunk[key]
            if chunk.get("usage"):
                meta["usage"] = _usage_summary(chunk["usage"])
            choices = chunk.get("choices") or []
            delta = (choices[0].get("delta") or {}) if choices else {}
            if not delta:
                continue
            if start is not None and "ttft_s" not in meta and any(
                delta.get(k) for k in ("content", "reasoning", "reasoning_content", "tool_calls")
            ):
                meta["ttft_s"] = round(now() - start, 3)
            text = delta.get("content")
            if text:
                content_parts.append(text)
                if on_token is not None:
                    on_token(text)
            for tc in delta.get("tool_calls", []) or []:
                tool_acc.add(tc)
            # OpenRouter streams `reasoning`; DeepSeek's API and vLLM use
            # `reasoning_content`.
            thought = delta.get("reasoning") or delta.get("reasoning_content")
            if thought:
                reasoning_parts.append(thought)
            for detail in delta.get("reasoning_details", []) or []:
                details_acc.add(detail)

        if start is not None:
            meta["duration_s"] = round(now() - start, 3)
        return Message(
            role="assistant",
            content="".join(content_parts),
            tool_calls=tool_acc.finish(),
            reasoning="".join(reasoning_parts),
            reasoning_details=details_acc.finish(),
            meta=meta,
        )


class _RetryableError(ProviderError):
    """A failure worth retrying: stalled stream, dropped connection, 429, 5xx."""


def _usage_summary(usage: dict[str, Any]) -> dict[str, Any]:
    """The usage fields worth keeping from a final stream chunk."""

    completion = usage.get("completion_tokens_details") or {}
    prompt = usage.get("prompt_tokens_details") or {}
    summary = {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "reasoning_tokens": completion.get("reasoning_tokens"),
        "cached_tokens": prompt.get("cached_tokens"),
        "cost": usage.get("cost"),
    }
    return {k: v for k, v in summary.items() if v is not None}


def _parse_sse_chunk(line: str) -> dict[str, Any] | None:
    """Parse one server-sent-events line into its JSON chunk, if any."""

    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    """Extract the choice delta from one server-sent-events line, if any."""

    chunk = _parse_sse_chunk(line)
    if chunk is None:
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


class _ReasoningDetailsAccumulator:
    """Merges streamed `reasoning_details` chunks back into whole blocks.

    Each chunk carries a fragment of one block, keyed by `index`; text-like fields
    concatenate in order and the rest (type, format, id) keep their last value.
    The merged blocks are what OpenRouter expects back, unmodified.
    """

    _APPEND = ("text", "summary", "data", "signature")

    def __init__(self) -> None:
        self._by_index: dict[int, dict[str, Any]] = {}
        self._order: list[int] = []

    def add(self, fragment: dict[str, Any]) -> None:
        index = fragment.get("index", 0)
        block = self._by_index.get(index)
        if block is None:
            self._by_index[index] = dict(fragment)
            self._order.append(index)
            return
        for key, value in fragment.items():
            if value is None:
                continue
            if key in self._APPEND and isinstance(value, str):
                block[key] = (block.get(key) or "") + value
            else:
                block[key] = value

    def finish(self) -> list[dict[str, Any]]:
        return [self._by_index[index] for index in self._order]
