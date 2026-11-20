"""Shared helpers for experiments.

`config(exp_dir)` loads base.toml and points memory and jobs at the
experiment's own runs/ folder. `agent(cfg)` is the stock `build_agent`; build
your own Agent in run.py when you want to swap a part.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

# Use this checkout's nod. `uv run --with-editable ../nod-extras` pulls in
# nod-extras' own dependency on nod, which is the main checkout's, so without
# this an experiment silently runs main's core instead of this branch's.
sys.path.insert(0, str(REPO / "src"))

from nod import config as config_module  # noqa: E402
from nod.agent import Agent  # noqa: E402
from nod.cli import build_agent  # noqa: E402
from nod.config import Config  # noqa: E402

if not Path(sys.modules["nod"].__file__).resolve().is_relative_to(REPO):
    raise ImportError(
        f"nod was already imported from {sys.modules['nod'].__file__}, not {REPO}; "
        "import lab before anything that imports nod"
    )


def config(exp_dir: str | Path, **overrides) -> Config:
    """base.toml, with state kept under `<exp_dir>/runs/`. Keyword args override fields."""

    runs = Path(exp_dir).resolve() / "runs"
    cfg = config_module.load(HERE / "base.toml")
    cfg = dataclasses.replace(
        cfg,
        soul_path=str(REPO / "SOUL.md"),
        memory_dir=str(runs / "memory"),
        jobs_dir=str(runs / "jobs"),
        skills_dirs=[str(REPO.parent / "nod-extras" / "skills")],
    )
    return dataclasses.replace(cfg, **overrides)


def agent(cfg: Config) -> Agent:
    return build_agent(cfg)


def stream(token: str) -> None:
    """An `on_token` callback that prints as the reply arrives."""

    print(token, end="", flush=True)
