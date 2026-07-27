"""The read tool: numbered, bounded text-file reads."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState, resolve_path, text_result
from pi_coding_agent.truncate import truncate_head


class ReadParameters(BaseModel):
    path: str = Field(description="Path to the file to read, relative to the working directory or absolute")
    offset: int = Field(default=1, ge=1, description="1-based first line to return")
    limit: int | None = Field(default=None, ge=1, description="Maximum number of lines to return")


class ReadTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="read",
            label="Read",
            description="Read a UTF-8 text file with cat -n style line numbers. Use offset and limit for large files.",
            parameters=ReadParameters.model_json_schema(),
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = ReadParameters.model_validate(params)
        path = resolve_path(self.cwd, values.path)
        self._check_abort(signal)
        if not await asyncio.to_thread(path.is_file):
            raise FileNotFoundError(f"File not found: {values.path}")
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".pdf"}:
            self.state.mark_read(path)
            return text_result(f"Binary file skipped: {values.path}")
        raw = await asyncio.to_thread(path.read_bytes)
        self._check_abort(signal)
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        start = values.offset - 1
        if start >= len(lines) and lines:
            raise ValueError(f"Offset {values.offset} is beyond end of file ({len(lines)} lines total)")
        selected = lines[start:] if values.limit is None else lines[start : start + values.limit]
        numbered = "\n".join(f"{number:>6}\t{line}" for number, line in enumerate(selected, values.offset))
        truncated = truncate_head(numbered)
        output = truncated.content
        if truncated.truncated:
            next_offset = values.offset + truncated.output_lines
            output += f"\n\n[Output truncated. Use offset={next_offset} to continue.]"
        elif values.limit is not None and start + len(selected) < len(lines):
            output += f"\n\n[{len(lines) - start - len(selected)} more lines. Use offset={values.offset + len(selected)} to continue.]"
        self.state.mark_read(path)
        return text_result(
            output,
            {"path": str(path), "line_count": len(lines), "truncation": truncated},
        )
