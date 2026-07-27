"""Built-in tool registration for the Python coding agent."""

from __future__ import annotations

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


def create_default_tools(cwd: str | Path) -> list[AgentTool]:
    """Create the six 3.1 tools with one shared read-before-mutation state."""
    state = ToolState()
    return [
        BashTool(cwd, state),
        ReadTool(cwd, state),
        WriteTool(cwd, state),
        EditTool(cwd, state),
        GrepTool(cwd, state),
        GlobTool(cwd, state),
        SearchTool(),   #非系统工具，不需要cwd和state
    ]


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
]
