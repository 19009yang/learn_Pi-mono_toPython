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


def _load_skill_from_dir(directory: Path) -> Skill | None:
    """Try to load a single ``SKILL.md`` from *directory*.

    Returns ``None`` if the file is missing or has no valid description.
    """
    skill_path = directory / "SKILL.md"
    if not skill_path.is_file():
        return None
    metadata, content = _parse_frontmatter(skill_path.read_text(encoding="utf-8"))
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip():
        return None
    name = metadata.get("name")
    skill_name = name.strip() if isinstance(name, str) and name.strip() else directory.name
    disabled = metadata.get("disable-model-invocation", metadata.get("disable_model_invocation", False))
    return Skill(
        name=skill_name,
        description=description.strip(),
        content=content,
        file_path=skill_path.resolve(),
        disable_model_invocation=disabled is True,
    )


def _discover_skill_dirs(root: Path) -> list[Path]:
    """Recursively discover directories that contain a ``SKILL.md``.

    Skips directories whose names start with ``.`` (e.g. ``.git``) or are
    ``__pycache__``.  *root* itself is included if it contains ``SKILL.md``.
    """
    _SKIP_DIRS = {".", "__pycache__"}
    result: list[Path] = []
    if (root / "SKILL.md").is_file():
        result.append(root)
    for path in sorted(root.rglob("SKILL.md")):
        parent = path.parent
        # Skip if any segment starts with "." or is __pycache__
        if any(part.startswith(".") or part in _SKIP_DIRS for part in parent.relative_to(root).parts):
            continue
        if parent not in result:
            result.append(parent)
    return result


def load_skills(directories: str | Path | Sequence[str | Path]) -> list[Skill]:
    """Load valid ``SKILL.md`` files from supplied directories, recursively.

    Each directory is scanned recursively: if it contains a ``SKILL.md`` it is
    loaded, and all sub-directories that contain a ``SKILL.md`` are also
    discovered and loaded.  Directories whose names start with ``.`` or are
    ``__pycache__`` are skipped.
    """
    skills: list[Skill] = []
    for directory in _as_directories(directories):
        for skill_dir in _discover_skill_dirs(directory):
            skill = _load_skill_from_dir(skill_dir)
            if skill is not None:
                skills.append(skill)
    return skills


def format_skill_invocation(skill: Skill, additional_instructions: str | None = None) -> str:
    """Construct the user message that invokes a loaded skill."""
    skill_block = (
        f'<skill name="{escape(skill.name)}" location="{escape(str(skill.file_path))}">\n'
        f"References are relative to {skill.file_path.parent}.\n\n{skill.content}\n</skill>"
    )
    return f"{skill_block}\n\n{additional_instructions}" if additional_instructions else skill_block
