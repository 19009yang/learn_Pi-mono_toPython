"""Phase 3.2 tests for Skill loading and system-prompt construction."""

from __future__ import annotations

from pathlib import Path

import pytest

from pi_coding_agent.skills import Skill, format_skill_invocation, load_skills
from pi_coding_agent.system_prompt import build_system_prompt
from pi_coding_agent.tools import LoadSkillTool, create_default_tools


def test_load_skills_reads_root_and_nested_skill_files(tmp_path: Path) -> None:
    skills_dir = tmp_path / "review"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review code carefully\ndisable-model-invocation: false\n---\nRead the target first.",
        encoding="utf-8",
    )
    nested = skills_dir / "nested"
    nested.mkdir()
    (nested / "SKILL.md").write_text(
        "---\nname: nested-review\ndescription: Nested review skill\n---\nCheck nested files.",
        encoding="utf-8",
    )

    skills = load_skills([skills_dir])

    assert len(skills) == 2
    assert skills[0].name == "review"
    assert skills[0].content == "Read the target first."
    assert skills[1].name == "nested-review"
    assert skills[1].content == "Check nested files."


def test_load_skills_skips_hidden_and_pycache_dirs(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills-root"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text(
        "---\nname: root\ndescription: Root skill\n---\nRoot content.",
        encoding="utf-8",
    )
    hidden = skills_dir / ".hidden"
    hidden.mkdir()
    (hidden / "SKILL.md").write_text(
        "---\nname: hidden\ndescription: Should be skipped\n---\nHidden content.",
        encoding="utf-8",
    )
    pycache = skills_dir / "__pycache__"
    pycache.mkdir()
    (pycache / "SKILL.md").write_text(
        "---\nname: pycache\ndescription: Should be skipped\n---\nPycache content.",
        encoding="utf-8",
    )
    visible = skills_dir / "visible"
    visible.mkdir()
    (visible / "SKILL.md").write_text(
        "---\nname: visible\ndescription: Visible skill\n---\nVisible content.",
        encoding="utf-8",
    )

    skills = load_skills([skills_dir])

    assert len(skills) == 2
    names = [s.name for s in skills]
    assert "root" in names
    assert "visible" in names
    assert "hidden" not in names
    assert "pycache" not in names


def test_load_skills_skips_invalid_frontmatter(tmp_path: Path) -> None:
    skills_dir = tmp_path / "bad-skill"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text(
        "---\nname: bad\n---\nNo description means this is skipped.",
        encoding="utf-8",
    )
    good = tmp_path / "good-skill"
    good.mkdir()
    (good / "SKILL.md").write_text(
        "---\nname: good\ndescription: A valid skill\n---\nGood content.",
        encoding="utf-8",
    )

    skills = load_skills([skills_dir, good])

    assert len(skills) == 1
    assert skills[0].name == "good"


def test_skill_invocation_and_system_prompt_include_xml(tmp_path: Path) -> None:
    skills_dir = tmp_path / "review"
    skills_dir.mkdir()
    (skills_dir / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review <code>\n---\nUse & keep the scope narrow.",
        encoding="utf-8",
    )
    skill = load_skills(skills_dir)[0]
    prompt = build_system_prompt(
        [skill],
        tmp_path,
        create_default_tools(tmp_path, [skill]),
        {"AGENTS.md": "Do not commit."},
    )

    assert "<available_skills>" in prompt
    assert "Review &lt;code&gt;" in prompt
    assert "<project_instructions path=\"AGENTS.md\">" in prompt
    assert "Read an existing file before overwriting or editing it." in prompt
    assert "call load_skill before following it" in prompt
    invocation = format_skill_invocation(skill, "Review this change.")
    assert '<skill name="review"' in invocation
    assert invocation.endswith("Review this change.")


def test_empty_skill_list_is_represented_in_system_prompt(tmp_path: Path) -> None:
    prompt = build_system_prompt([], tmp_path, [])
    assert "<available_skills>\n</available_skills>" in prompt


@pytest.mark.asyncio
async def test_load_skill_tool_returns_full_loaded_instructions(tmp_path: Path) -> None:
    skill = Skill(
        name="review",
        description="Review code",
        content="Read the target before proposing changes.",
        file_path=tmp_path / "SKILL.md",
    )
    tool = LoadSkillTool([skill])

    result = await tool.execute("call-1", {"name": "review"})

    assert tool.parameters["properties"]["name"]["enum"] == ["review"]
    assert '<skill name="review"' in result.content[0].text
    assert "Read the target before proposing changes." in result.content[0].text
