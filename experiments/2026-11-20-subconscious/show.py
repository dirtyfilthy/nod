"""Replay a session as the subconscious saw it, thoughts highlighted.

  show.py              the newest session
  show.py <session>    a given one
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from subconscious import render  # noqa: E402

from nod.memory import JSONFileMemory  # noqa: E402

memory_dir = HERE / "runs" / "memory"
if len(sys.argv) > 1:
    sid = sys.argv[1]
else:
    sid = max(memory_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime).stem

print(f"# {sid}\n")
for block in render(JSONFileMemory(memory_dir).load(sid)).split("\n\n"):
    if block.startswith("THOUGHT"):
        print(f"\033[1;35m{block}\033[0m\n")
    elif block.startswith(("RESULT", "NOD (thinking)")):
        print(f"\033[2m{block}\033[0m\n")
    else:
        print(f"{block}\n")
