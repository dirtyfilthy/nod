"""Run one built-in tool call inside the sandbox container.

The host side (`sandbox.py`) runs `python -m nod.toolhost` with `docker exec`,
writes `{"name": ..., "arguments": {...}}` to stdin and reads `{"result": ...}`
from stdout. Dispatch goes through a normal `ToolRegistry`, so unknown tools,
bad arguments and exceptions come back as the same "error: ..." strings they
would on the host.
"""

from __future__ import annotations

import json
import sys

from nod.builtins import builtin_tools
from nod.messages import ToolCall
from nod.tools import ToolRegistry


def main() -> int:
    request = json.load(sys.stdin)
    call = ToolCall(id="sandbox", name=request["name"], arguments=request["arguments"])
    result = ToolRegistry(builtin_tools()).dispatch(call)
    json.dump({"result": result}, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
