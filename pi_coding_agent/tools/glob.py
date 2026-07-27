"""Glob tool sorted by newest modification time first."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState, resolve_path, text_result
from pi_coding_agent.truncate import truncate_head


class GlobParameters(BaseModel):
    pattern: str = Field(description="Pathlib glob pattern, for example **/*.py")
    path: str = Field(default=".", description="Directory to search")
    limit: int = Field(default=1_000, ge=1, le=10_000, description="Maximum paths to return")


class GlobTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="glob",
            label="Glob",
            description="Find files matching a glob pattern, ordered by modification time with newest first.",
            parameters=GlobParameters.model_json_schema(),
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = GlobParameters.model_validate(params)
        root = resolve_path(self.cwd, values.path)
        self._check_abort(signal)
        if not await asyncio.to_thread(root.is_dir):
            raise NotADirectoryError(f"Directory not found: {values.path}")
        paths = await asyncio.to_thread(lambda: [path for path in root.glob(values.pattern) if path.is_file()])
        paths.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        selected = paths[: values.limit]
        output = "\n".join(str(path.relative_to(root)).replace("\\", "/") for path in selected)
        if not output:
            return text_result("No files found matching pattern")
        truncation = truncate_head(output, max_lines=values.limit)
        if len(paths) > values.limit:
            output = f"{truncation.content}\n\n[{values.limit} result limit reached.]"
        else:
            output = truncation.content
        return text_result(output, {"matches": len(paths), "truncation": truncation})
