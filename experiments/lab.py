"""Shared helpers for experiments.

`config(exp_dir)` loads base.toml and points memory and jobs at the
experiment's own runs/ folder. `agent(cfg)` is the stock `build_agent`; build
your own Agent in run.py when you want to swap a part.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

from nod import config as config_module
from nod.agent import Agent
from nod.cli import build_agent
from nod.config import Config

HERE = Path(__file__).resolve().parent
REPO = HERE.parent


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
