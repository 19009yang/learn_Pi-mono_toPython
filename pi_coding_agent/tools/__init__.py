"""Built-in tool registration for the Python coding agent."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from pi_agent.types import AgentTool
from pi_coding_agent.tools.base import ToolState
from pi_coding_agent.tools.bash import BashTool
from pi_coding_agent.tools.edit import EditTool
from pi_coding_agent.tools.glob import GlobTool
from pi_coding_agent.tools.grep import GrepTool
from pi_coding_agent.tools.read import ReadTool
from pi_coding_agent.tools.write import WriteTool
from pi_coding_agent.tools.search import SearchTool
from pi_coding_agent.tools.load_skill import LoadSkillTool
from pi_coding_agent.skills import Skill


def create_default_tools(cwd: str | Path, skills: Sequence[Skill] = ()) -> list[AgentTool]:
    """Create coding tools and, when present, a model-invokable Skill loader."""
    state = ToolState()
    tools: list[AgentTool] = [
        BashTool(cwd, state),
        ReadTool(cwd, state),
        WriteTool(cwd, state),
        EditTool(cwd, state),
        GrepTool(cwd, state),
        GlobTool(cwd, state),
        SearchTool(),   #非系统工具，不需要cwd和state
    ]
    invokable_skills = [skill for skill in skills if not skill.disable_model_invocation]
    if invokable_skills:
        tools.append(LoadSkillTool(invokable_skills))
    return tools


__all__ = [
    "BashTool",
    "EditTool",
    "GlobTool",
    "GrepTool",
    "ReadTool",
    "ToolState",
    "WriteTool",
    "SearchTool",
    "create_default_tools",
    "LoadSkillTool",
]
