"""Exact string replacement tool with a read-before-edit guard."""

from __future__ import annotations

import asyncio
from pathlib import Path

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState, resolve_path, text_result


class EditParameters(BaseModel):
    path: str = Field(description="Path to the file to edit")
    old_string: str = Field(description="Exact text to replace")
    new_string: str = Field(description="Replacement text")
    replace_all: bool = Field(default=False, description="Replace all matches instead of requiring exactly one")


class EditTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="edit",
            label="Edit",
            description="Make an exact text replacement in a file that has first been read.",
            parameters=EditParameters.model_json_schema(),
        )

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = EditParameters.model_validate(params)
        if not values.old_string:
            raise ValueError("old_string must not be empty")
        path = resolve_path(self.cwd, values.path)
        self._check_abort(signal)
        if not await asyncio.to_thread(path.is_file):
            raise FileNotFoundError(f"File not found: {values.path}")
        if not self.state.was_read(path):
            raise PermissionError(f"Read {values.path} before editing it")
        content = await asyncio.to_thread(path.read_text, encoding="utf-8")
        count = content.count(values.old_string)
        if count == 0:
            raise ValueError("old_string was not found in the file")
        if not values.replace_all and count != 1:
            raise ValueError(f"old_string must have exactly one match; found {count}. Use replace_all to replace all matches.")
        replacements = count if values.replace_all else 1
        updated = content.replace(values.old_string, values.new_string, -1 if values.replace_all else 1)
        self._check_abort(signal)
        await asyncio.to_thread(path.write_text, updated, encoding="utf-8")
        self.state.mark_read(path)
        return text_result(
            f"Successfully replaced {replacements} occurrence(s) in {values.path}",
            {"path": str(path), "replacements": replacements},
        )
