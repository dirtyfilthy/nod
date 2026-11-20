import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import lab  # noqa: E402

cfg = lab.config(HERE)
agent = lab.agent(cfg)

reply = agent.send("Say hello in five words.", session_id="exp", on_token=lab.stream)
print()
