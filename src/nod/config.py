"""Configuration: env plus an optional TOML file.

Secrets never live in the config file. The API key is read from the environment by
the provider. Everything here is plumbing: which model, where to find skills, where
to keep memory.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_NAMES = ("config.toml",)
API_TOKEN_ENV = "NOD_API_TOKEN"
BUNDLED_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass
class Config:
    model: str = "deepseek/deepseek-v4.1-flash-1017"
    base_url: str = "https://openrouter.ai/api/v1"
    max_turns: int = 20
    soul_path: str = "SOUL.md"
    memory_dir: str = "~/.nod/memory"
    skills_dirs: list[str] = field(default_factory=lambda: ["../nod-extras/skills"])
    # HTTP service (`nod serve`). Loopback by default; front it with a reverse
    # proxy or tunnel to expose it. The auth token is read from the environment,
    # never from here -- see Config.api_token().
    host: str = "127.0.0.1"
    port: int = 8080
    compaction_enabled: bool = True
    context_window: int = 512_000
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    # Directory the config was loaded from; relative paths resolve against it.
    base_dir: Path = field(default_factory=Path.cwd)

    def resolve(self, path: str) -> Path:
        p = Path(path).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    def soul(self) -> str:
        p = self.resolve(self.soul_path)
        return p.read_text(encoding="utf-8") if p.exists() else ""

    def resolved_skills_dirs(self) -> list[Path]:
        dirs = [self.resolve(d) for d in self.skills_dirs]
        if BUNDLED_SKILLS_DIR not in dirs:
            # Appended last so configured/user skills with the same name win.
            dirs.append(BUNDLED_SKILLS_DIR)
        return dirs

    def api_token(self) -> str:
        """Bearer token required by the HTTP service, read from the environment."""

        return os.environ.get(API_TOKEN_ENV, "")


def load(path: str | Path | None = None) -> Config:
    """Load config from an explicit path, a discovered `config.toml`, or defaults."""

    config_path = _find_config(path)
    if config_path is None:
        return Config()
    raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    cfg = Config(base_dir=config_path.parent)
    for key in (
        "model",
        "base_url",
        "max_turns",
        "soul_path",
        "memory_dir",
        "skills_dirs",
        "host",
        "port",
    ):
        if key in raw:
            setattr(cfg, key, raw[key])
    compaction = raw.get("compaction", {})
    if isinstance(compaction, dict):
        mapping = {
            "enabled": "compaction_enabled",
            "context_window": "context_window",
            "reserve_tokens": "reserve_tokens",
            "keep_recent_tokens": "keep_recent_tokens",
        }
        for source, target in mapping.items():
            if source in compaction:
                setattr(cfg, target, compaction[source])
    return cfg


def _find_config(path: str | Path | None) -> Path | None:
    if path is not None:
        p = Path(path).expanduser()
        return p if p.exists() else None
    for name in DEFAULT_CONFIG_NAMES:
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate
    return None
