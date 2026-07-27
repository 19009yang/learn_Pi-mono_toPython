"""Minimal Skill loading and prompt formatting for the coding-agent MVP."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence
from xml.sax.saxutils import escape


@dataclass(frozen=True)
class Skill:
    """A skill's metadata and full Markdown instructions."""

    name: str
    description: str
    content: str
    file_path: Path
    disable_model_invocation: bool = False


def _parse_yaml_scalar(value: str) -> str | bool:
    stripped = value.strip()
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _parse_frontmatter(source: str) -> tuple[dict[str, str | bool], str]:
    """Parse the scalar YAML subset used by MVP SKILL.md frontmatter."""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"^---\n(?P<frontmatter>.*?)\n---(?:\n|$)", normalized, re.DOTALL)
    if match is None:
        return {}, normalized
    values: dict[str, str | bool] = {}
    lines = match.group("frontmatter").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            index += 1
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if raw_value in {"|", ">"}:
            block: list[str] = []
            index += 1
            while index < len(lines) and (lines[index].startswith(" ") or lines[index].startswith("\t")):
                block.append(lines[index].lstrip())
                index += 1
            values[key] = "\n".join(block) if raw_value == "|" else " ".join(block)
            continue
        values[key] = _parse_yaml_scalar(raw_value)
        index += 1
    return values, normalized[match.end() :].strip()


def _as_directories(directories: str | Path | Sequence[str | Path]) -> list[Path]:
    if isinstance(directories, (str, Path)):
        return [Path(directories)]
    return [Path(directory) for directory in directories]


def load_skills(directories: str | Path | Sequence[str | Path]) -> list[Skill]:
    """Load valid root ``SKILL.md`` files from explicitly supplied directories.

    This MVP deliberately skips recursive discovery and ignore-file handling;
    those can be added without changing the Skill or prompt interfaces.
    """
    skills: list[Skill] = []
    for directory in _as_directories(directories):
        skill_path = directory / "SKILL.md"
        if not skill_path.is_file():
            continue
        metadata, content = _parse_frontmatter(skill_path.read_text(encoding="utf-8"))
        description = metadata.get("description")
        if not isinstance(description, str) or not description.strip():
            continue
        name = metadata.get("name")
        skill_name = name.strip() if isinstance(name, str) and name.strip() else directory.name
        disabled = metadata.get("disable-model-invocation", metadata.get("disable_model_invocation", False))
        skills.append(
            Skill(
                name=skill_name,
                description=description.strip(),
                content=content,
                file_path=skill_path.resolve(),
                disable_model_invocation=disabled is True,
            )
        )
    return skills


def format_skill_invocation(skill: Skill, additional_instructions: str | None = None) -> str:
    """Construct the user message that invokes a loaded skill."""
    skill_block = (
        f'<skill name="{escape(skill.name)}" location="{escape(str(skill.file_path))}">\n'
        f"References are relative to {skill.file_path.parent}.\n\n{skill.content}\n</skill>"
    )
    return f"{skill_block}\n\n{additional_instructions}" if additional_instructions else skill_block
