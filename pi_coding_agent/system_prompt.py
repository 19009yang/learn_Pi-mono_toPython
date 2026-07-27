"""System prompt assembly for the Python coding-agent MVP."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from xml.sax.saxutils import escape

from pi_agent.types import AgentTool
from pi_coding_agent.skills import Skill


def _tool_description(tool: AgentTool | str) -> tuple[str, str]:
    if isinstance(tool, str):
        return tool, ""
    return tool.name, tool.description


def _format_skills(skills: Sequence[Skill]) -> str:
    lines = [
        "<available_skills>",
    ]
    for skill in skills:
        if skill.disable_model_invocation:
            continue
        lines.extend(
            [
                "  <skill>",
                f"    <name>{escape(skill.name)}</name>",
                f"    <description>{escape(skill.description)}</description>",
                f"    <location>{escape(str(skill.file_path))}</location>",
                "  </skill>",
            ]
        )
    lines.append("</available_skills>")
    return "\n".join(lines)


def _format_project_context(project_context: str | Mapping[str, str] | None) -> str:
    if project_context is None:
        return ""
    if isinstance(project_context, str):
        return f"\n\n<project_context>\n{project_context}\n</project_context>"
    lines = ["", "", "<project_context>"]
    for path, content in project_context.items():
        lines.append(f'<project_instructions path="{escape(path)}">')
        lines.append(content)
        lines.append("</project_instructions>")
    lines.append("</project_context>")
    return "\n".join(lines)


def build_system_prompt(
    skills: Sequence[Skill],
    cwd: str | Path,
    tools: Sequence[AgentTool | str],
    project_context: str | Mapping[str, str] | None = None,
) -> str:
    """Build a stable coding-agent prompt while keeping Skills pluggable."""
    rendered_tools = [
        f"- {name}: {description}" if description else f"- {name}"
        for name, description in (_tool_description(tool) for tool in tools)
    ]
    tools_block = "\n".join(rendered_tools) if rendered_tools else "- (none)"
    prompt = f"""You are pi, an expert coding assistant working in a local project.

Working directory: {Path(cwd).resolve()}

Available tools:
{tools_block}

Tool use rules:
- Inspect relevant files before making changes.
- Read an existing file before overwriting or editing it.
- Prefer the dedicated read, grep, glob, write, and edit tools over shell equivalents.
- Explain completed work concisely and include relevant file paths.

Skills provide task-specific instructions. When a task matches a skill description, read its full SKILL.md file before following it.
{_format_skills(skills)}"""
    return prompt + _format_project_context(project_context)
