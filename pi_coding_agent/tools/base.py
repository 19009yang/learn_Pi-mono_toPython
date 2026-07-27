"""Shared state and helpers for the Phase 3.1 filesystem tools."""

from __future__ import annotations

from pathlib import Path

from pi_ai.event_stream import AbortSignal
from pi_ai.types import TextContent
from pi_agent.types import AgentTool, AgentToolResult, AgentToolUpdateCallback


class ToolState:
    """Process-local safety state shared by read, write, and edit tools."""

    def __init__(self) -> None:
        self.read_files: set[Path] = set()

    def mark_read(self, path: Path) -> None:
        self.read_files.add(path.resolve())

    def was_read(self, path: Path) -> bool:
        return path.resolve() in self.read_files


def resolve_path(cwd: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def text_result(text: str, details: dict[str, object] | None = None) -> AgentToolResult[dict[str, object] | None]:
    return AgentToolResult(content=[TextContent(text=text)], details=details)


class CodingTool(AgentTool):
    """Base class that owns a working directory and shared tool state."""

    def __init__(
        self,
        *,
        cwd: str | Path,
        state: ToolState,
        name: str,
        label: str,
        description: str,
        parameters: dict[str, object],
    ) -> None:
        super().__init__(
            name=name,
            label=label,
            description=description,
            parameters=parameters,
        )
        self.cwd = Path(cwd).resolve()
        self.state = state

    @staticmethod
    def _check_abort(signal: AbortSignal | None) -> None:
        if signal is not None and signal.aborted:
            raise RuntimeError("Operation aborted")

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        raise NotImplementedError
