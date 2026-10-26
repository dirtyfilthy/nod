"""Skills: markdown instructions, loaded on demand.

A skill is a `SKILL.md` file with a tiny frontmatter block (`name`, `description`).
Nod only ever sees the index -- one line per skill -- in his system prompt. That
stays cheap even with thousands of skills. When a task matches, he calls the
built-in `read_skill` tool to pull the full text. This is progressive disclosure:
the model decides what to load instead of drowning in everything up front.

No YAML dependency: the frontmatter parser handles the simple `key: value` block
that skills use and nothing more.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nod.tools import Tool

SKILL_FILENAME = "SKILL.md"


@dataclass
class Skill:
    name: str
    description: str
    path: Path
    body: str


class SkillLoader:
    """Discovers skills under a set of directories and exposes them to the agent."""

    def __init__(self, skills_dirs: list[Path | str] | None = None) -> None:
        self._dirs = [Path(d).expanduser() for d in (skills_dirs or [])]
        self._by_name: dict[str, Skill] = {}
        self.reload()

    def reload(self) -> None:
        # Build the new index and swap it in with one assignment: the loader is
        # shared by every agent in the process, and a reader on another thread
        # must never see a half-filled dict.
        by_name: dict[str, Skill] = {}
        for root in self._dirs:
            if not root.is_dir():
                continue
            for skill_file in sorted(root.rglob(SKILL_FILENAME)):
                skill = _parse_skill(skill_file)
                if skill is not None:
                    # First definition of a name wins; warn-free, deterministic.
                    by_name.setdefault(skill.name, skill)
        self._by_name = by_name

    @property
    def skills(self) -> list[Skill]:
        self.reload()
        return list(self._by_name.values())

    def get(self, name: str) -> Skill | None:
        self.reload()
        return self._by_name.get(name)

    def index_text(self) -> str:
        """The compact catalogue injected into the system prompt."""
        self.reload()

        if not self._by_name:
            return ""
        lines = ["Available skills (call read_skill with the name to load full instructions):"]
        for skill in self._by_name.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)

    def read_skill_tool(self) -> Tool:
        """The built-in tool that loads a skill's full markdown on demand."""

        def read_skill(name: str) -> str:
            skill = self.get(name)
            if skill is None:
                known = ", ".join(self._by_name) or "(none)"
                return f"error: no skill named '{name}'. Known skills: {known}"
            return skill.body

        return Tool(
            name="read_skill",
            description=(
                "Load the full instructions for a skill by name. Use this when a "
                "task matches one of the available skills before attempting it."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The skill name from the available skills list.",
                    }
                },
                "required": ["name"],
            },
            func=read_skill,
        )


def _parse_skill(path: Path) -> Skill | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    meta, body = _split_frontmatter(raw)
    name = meta.get("name") or path.parent.name
    description = meta.get("description", "")
    return Skill(name=name, description=description, path=path, body=body)


def _split_frontmatter(raw: str) -> tuple[dict[str, str], str]:
    """Return (frontmatter dict, body) for a `--- ... ---` prefixed document."""

    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, raw
    meta: dict[str, str] = {}
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
        key, sep, value = lines[i].partition(":")
        if sep:
            meta[key.strip()] = value.strip().strip("'\"")
    if end is None:
        return {}, raw
    body = "\n".join(lines[end + 1 :]).strip()
    return meta, body
