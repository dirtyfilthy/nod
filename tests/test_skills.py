from __future__ import annotations

from pathlib import Path

from nod.messages import ToolCall
from nod.skills import SkillLoader


def _write_skill(root: Path, folder: str, name: str, description: str, body: str) -> None:
    d = root / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_loads_and_indexes_skills(tmp_path: Path):
    _write_skill(tmp_path, "deploy", "deploy-checklist", "Pre-ship steps.", "1. test")
    _write_skill(tmp_path, "backup", "backup-db", "How to back up.", "pg_dump ...")

    loader = SkillLoader([tmp_path])
    names = sorted(s.name for s in loader.skills)
    assert names == ["backup-db", "deploy-checklist"]

    index = loader.index_text()
    assert "deploy-checklist: Pre-ship steps." in index
    assert "backup-db: How to back up." in index


def test_read_skill_tool_returns_body(tmp_path: Path):
    _write_skill(tmp_path, "deploy", "deploy-checklist", "Pre-ship steps.", "step one")
    loader = SkillLoader([tmp_path])
    rs = loader.read_skill_tool()

    out = rs.func(name="deploy-checklist")
    assert "step one" in out
    assert "Pre-ship steps." not in out  # frontmatter is stripped from the body


def test_read_skill_unknown(tmp_path: Path):
    loader = SkillLoader([tmp_path])
    rs = loader.read_skill_tool()
    out = rs.func(name="ghost")
    assert "no skill named 'ghost'" in out


def test_no_skills_means_empty_index(tmp_path: Path):
    loader = SkillLoader([tmp_path])
    assert loader.index_text() == ""


def test_missing_frontmatter_falls_back_to_folder_name(tmp_path: Path):
    d = tmp_path / "loose"
    d.mkdir()
    (d / "SKILL.md").write_text("just a body, no frontmatter", encoding="utf-8")
    loader = SkillLoader([tmp_path])
    skill = loader.get("loose")
    assert skill is not None
    assert skill.body == "just a body, no frontmatter"


def test_dispatch_read_skill_via_registry(tmp_path: Path):
    from nod.tools import ToolRegistry

    _write_skill(tmp_path, "deploy", "deploy-checklist", "Pre-ship.", "the steps")
    loader = SkillLoader([tmp_path])
    registry = ToolRegistry([loader.read_skill_tool()])
    out = registry.dispatch(
        ToolCall(id="1", name="read_skill", arguments={"name": "deploy-checklist"})
    )
    assert out == "the steps"
