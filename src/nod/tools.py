"""Tools: the small, separate concept.

A `Tool` is a named function with a JSON-schema description of its arguments. The
`ToolRegistry` hands those schemas to the provider and dispatches the calls the
model makes back to the right function. That's the whole contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from nod.messages import ToolCall


@dataclass
class Tool:
    """A callable the model can invoke.

    `func` receives the parsed arguments as keyword args and returns a string (the
    tool result the model will see). `parameters` is a JSON Schema object describing
    those arguments.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[..., str]

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def tool(
    name: str, description: str, parameters: dict[str, Any] | None = None
) -> Callable[[Callable[..., str]], Tool]:
    """Decorator sugar for defining a tool from a function."""

    def wrap(func: Callable[..., str]) -> Tool:
        return Tool(
            name=name,
            description=description,
            parameters=parameters or {"type": "object", "properties": {}},
            func=func,
        )

    return wrap


# Called by the agent loop before each tool call runs. Return None to let the
# call run, or a string the model reads as the call's result instead (a
# refusal and its reason). The hook runs inside the turn, so
# `nod.agent.current_session()` names the session. A hook that raises stops
# the call: the loop fails closed.
PreToolHook = Callable[[ToolCall], "str | None"]


def ALWAYS_ACCEPT(call: ToolCall) -> str | None:
    """The default pre-tool hook: every call runs. nod-extras can swap in a gate."""

    return None


class ToolRegistry:
    """Holds the tools available this run and routes calls to them."""

    def __init__(self, tools: list[Tool] | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        for t in tools or []:
            self.register(t)

    def register(self, t: Tool) -> None:
        self._tools[t.name] = t

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)

    def schemas(self) -> list[dict[str, Any]]:
        """Tool definitions in the format providers expect."""

        return [t.schema() for t in self._tools.values()]

    def dispatch(self, call: ToolCall) -> str:
        """Run one tool call and return its result as a string.

        Failures (unknown tool, bad arguments, raised exceptions) are turned into a
        string the model can read and recover from, rather than crashing the loop.
        """

        t = self._tools.get(call.name)
        if t is None:
            return f"error: no such tool '{call.name}'"
        if not isinstance(call.arguments, dict):
            return f"error: arguments for '{call.name}' were not an object"
        try:
            result = t.func(**call.arguments)
        except TypeError as exc:
            return f"error: bad arguments for '{call.name}': {exc}"
        except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash
            return f"error: tool '{call.name}' failed: {exc}"
        return result if isinstance(result, str) else json.dumps(result)
