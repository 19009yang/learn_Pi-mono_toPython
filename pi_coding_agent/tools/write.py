"""The write tool with read-before-overwrite protection."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState, resolve_path, text_result


class WriteParameters(BaseModel):
    path: str = Field(description="Path to create or overwrite")
    content: str = Field(description="Complete UTF-8 file content")


class WriteTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="write",
            label="Write",
            description="Create a file or overwrite a file that has first been read. Parent directories are created automatically.",
            parameters=WriteParameters.model_json_schema(),
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = WriteParameters.model_validate(params)
        path = resolve_path(self.cwd, values.path)
        self._check_abort(signal)
        exists = await asyncio.to_thread(path.exists)
        if exists and not self.state.was_read(path):
            raise PermissionError(f"Read {values.path} before overwriting it")
        await asyncio.to_thread(path.parent.mkdir, parents=True, exist_ok=True)
        self._check_abort(signal)
        await asyncio.to_thread(path.write_text, values.content, encoding="utf-8")
        self._check_abort(signal)
        self.state.mark_read(path)
        return text_result(
            f"Successfully wrote {len(values.content.encode('utf-8'))} bytes to {values.path}",
            {"path": str(path)},
        )
