"""Regex search tool that uses ripgrep when available and Python as fallback."""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState, resolve_path, text_result
from pi_coding_agent.truncate import truncate_head, truncate_line

GrepOutputMode = Literal["content", "files_with_matches", "count"]


class GrepParameters(BaseModel):
    pattern: str = Field(description="Regular expression or literal search text")
    path: str = Field(default=".", description="File or directory to search")
    glob: str | None = Field(default=None, description="Optional file glob filter")
    ignore_case: bool = Field(default=False, description="Case-insensitive search")
    literal: bool = Field(default=False, description="Treat pattern as literal text")
    output_mode: GrepOutputMode = Field(default="content", description="content, files_with_matches, or count")
    limit: int = Field(default=100, ge=1, le=10_000, description="Maximum matches or files")


class GrepTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="grep",
            label="Grep",
            description="Search text with ripgrep when available; long matching lines are shortened to 500 characters.",
            parameters=GrepParameters.model_json_schema(),
        )

    async def _rg_matches(self, values: GrepParameters, root: Path) -> list[tuple[Path, int, str]] | None:
        executable = shutil.which("rg")
        if executable is None:
            return None
        arguments = [executable, "--json", "--line-number", "--color=never", "--no-messages"]
        if values.ignore_case:
            arguments.append("--ignore-case")
        if values.literal:
            arguments.append("--fixed-strings")
        if values.glob:
            arguments.extend(["--glob", values.glob])
        arguments.extend(["--", values.pattern, str(root)])
        process = await asyncio.create_subprocess_exec(
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode not in (0, 1):
            raise RuntimeError(stderr.decode("utf-8", errors="replace").strip() or "ripgrep failed")
        matches: list[tuple[Path, int, str]] = []
        for line in stdout.decode("utf-8", errors="replace").splitlines():
            event = json.loads(line)
            if event.get("type") != "match":
                continue
            data = event["data"]
            matches.append(
                (
                    Path(data["path"]["text"]),
                    int(data["line_number"]),
                    data["lines"]["text"].rstrip("\r\n"),
                )
            )
        return matches

    async def _python_matches(self, values: GrepParameters, root: Path) -> list[tuple[Path, int, str]]:
        expression = re.escape(values.pattern) if values.literal else values.pattern
        flags = re.IGNORECASE if values.ignore_case else 0
        pattern = re.compile(expression, flags)
        paths = [root] if root.is_file() else (path for path in root.rglob("*") if path.is_file())
        matches: list[tuple[Path, int, str]] = []
        for path in paths:
            if any(part in {".git", "node_modules"} for part in path.parts):
                continue
            if values.glob and not fnmatch.fnmatch(path.name, values.glob) and not path.match(values.glob):
                continue
            try:
                text = await asyncio.to_thread(path.read_text, encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if pattern.search(line):
                    matches.append((path, number, line))
        return matches

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = GrepParameters.model_validate(params)
        root = resolve_path(self.cwd, values.path)
        self._check_abort(signal)
        if not await asyncio.to_thread(root.exists):
            raise FileNotFoundError(f"Path not found: {values.path}")
        matches = await self._rg_matches(values, root)
        if matches is None:
            matches = await self._python_matches(values, root)
        self._check_abort(signal)
        relative_root = root if root.is_dir() else root.parent
        if values.output_mode == "count":
            counts = Counter(path for path, _, _ in matches)
            lines = [f"{path.relative_to(relative_root)}:{count}" for path, count in sorted(counts.items())]
        elif values.output_mode == "files_with_matches":
            lines = [str(path.relative_to(relative_root)) for path in sorted({path for path, _, _ in matches})]
        else:
            lines = []
            for path, number, text in matches:
                shortened, _ = truncate_line(text)
                lines.append(f"{path.relative_to(relative_root)}:{number}: {shortened}")
        limited = lines[: values.limit]
        if not limited:
            return text_result("No matches found")
        truncation = truncate_head("\n".join(limited), max_lines=values.limit)
        output = truncation.content
        if len(lines) > values.limit:
            output += f"\n\n[{values.limit} result limit reached; refine the pattern or increase limit.]"
        return text_result(output, {"matches": len(matches), "truncation": truncation})
