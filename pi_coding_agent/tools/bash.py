"""Shell command tool with streaming updates, cancellation, and tail truncation."""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

from pydantic import BaseModel, Field

from pi_ai.event_stream import AbortSignal
from pi_ai.types import TextContent
from pi_agent.types import AgentToolResult, AgentToolUpdateCallback
from pi_coding_agent.tools.base import CodingTool, ToolState
from pi_coding_agent.truncate import DEFAULT_MAX_BYTES, truncate_tail


class BashParameters(BaseModel):
    command: str = Field(description="Shell command to run")
    timeout: float = Field(default=120.0, gt=0, le=600.0, description="Timeout in seconds")


def _find_bash() -> str | None:
    if sys.platform != "win32":
        return "/bin/bash" if Path("/bin/bash").is_file() else shutil.which("bash")
    candidates = [
        Path(os.environ.get("ProgramFiles", "")) / "Git" / "bin" / "bash.exe",
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Git" / "bin" / "bash.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("bash")


def _sanitize_output(text: str) -> str:
    return "".join(char for char in text if char in "\t\n\r" or (char.isprintable() and ord(char) != 127))


def _sanitize_command(command: str) -> tuple[str, list[str]]:
    """Pre-process a shell command to prevent accidental file redirections.

    On Windows (Git Bash), LLM-generated commands may contain patterns that
    bash interprets as file redirections, creating spurious files:

    - ``pip install pkg>=1.0``  → bash sees ``> =1.0`` (creates ``=1.0`` file)
    - ``command > nul``         → bash sees ``nul`` as a regular file (not NUL device)
    - ``command 2> nul``        → same issue

    Returns (sanitized_command, warnings).
    """
    import re

    warnings: list[str] = []

    # 1) Fix bare ``>=version`` in pip/uv install commands (e.g. pymupdf>=1.23.0)
    #    This is the most common case: the LLM writes ``pip install pkg>=1.0``
    #    and bash interprets ``>=1.0`` as ``> =1.0`` (redirect to file named ``=1.0``).
    #    We wrap the package specifier in quotes: ``pip install "pkg>=1.0"``
    def _fix_bare_ge(match: re.Match[str]) -> str:
        prefix = match.group(1)      # e.g. "pip install pymupdf"
        pkg_name = match.group(2)     # e.g. "pymupdf"
        version_spec = match.group(3) # e.g. ">=1.23.0"
        warnings.append(
            f"Auto-quoted bare version specifier: {pkg_name}{version_spec} → \"{pkg_name}{version_spec}\""
        )
        return f'{prefix}"{pkg_name}{version_spec}"'

    command = re.sub(
        r'((?:pip|uv|pipx)\s+install\s+)(\w[\w.-]*)([><=!~]=?[\w.*+-]+)',
        _fix_bare_ge,
        command,
    )

    # 2) Replace ``> nul`` / ``2> nul`` with ``> /dev/null`` / ``2> /dev/null``
    #    On Windows cmd.exe, ``> nul`` suppresses output; in Git Bash, ``nul``
    #    is a regular filename and creates a file named ``nul``.
    nul_pattern = re.compile(r'(\d?>|2>)\s*nul\b')
    if nul_pattern.search(command):
        command = nul_pattern.sub(r'\1/dev/null', command)
        warnings.append("Replaced '> nul' with '> /dev/null' (Git Bash compatible)")

    return command, warnings


class BashTool(CodingTool):
    def __init__(self, cwd: str | Path, state: ToolState | None = None) -> None:
        super().__init__(
            cwd=cwd,
            state=state or ToolState(),
            name="bash",
            label="Bash",
            description="Run a command in bash. Output is streamed and retains the final 50KB if truncated.",
            parameters=BashParameters.model_json_schema(),
        )
        self.bash_path = _find_bash()

    async def execute(
        self,
        tool_call_id: str,
        params: dict[str, object],
        signal: AbortSignal | None = None,
        on_update: AgentToolUpdateCallback | None = None,
    ) -> AgentToolResult[dict[str, object] | None]:
        values = BashParameters.model_validate(params)
        self._check_abort(signal)

        # Pre-process command to prevent accidental file redirections
        values.command, cmd_warnings = _sanitize_command(values.command)
        if cmd_warnings and on_update is not None:
            warn_text = "[bash sanitize] " + "; ".join(cmd_warnings)
            on_update(
                AgentToolResult(
                    content=[TextContent(text=warn_text)],
                    details={"partial": True, "sanitize_warning": True},
                )
            )

        if self.bash_path is None:
            raise RuntimeError("bash was not found; install Git Bash or add bash to PATH")
        if sys.platform == "win32":
            # ``create_subprocess_shell`` routes through cmd.exe on Windows,
            # which breaks Git Bash paths containing spaces. Invoke bash
            # directly while keeping the same shell command semantics.
            process = await asyncio.create_subprocess_exec(
                self.bash_path,
                "-lc",
                values.command,
                cwd=str(self.cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                values.command,
                cwd=str(self.cwd),
                executable=self.bash_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        assert process.stdout is not None
        chunks: list[bytes] = []
        total_bytes = 0

        async def read_output() -> None:
            nonlocal total_bytes
            while chunk := await process.stdout.read(4_096):
                total_bytes += len(chunk)
                chunks.append(chunk)
                if on_update is not None:
                    partial = _sanitize_output(b"".join(chunks).decode("utf-8", errors="replace"))
                    on_update(
                        AgentToolResult(
                            content=[TextContent(text=truncate_tail(partial, max_bytes=DEFAULT_MAX_BYTES).content)],
                            details={"partial": True},
                        )
                    )

        reader = asyncio.create_task(read_output())
        waiters: set[asyncio.Task[object]] = {asyncio.create_task(process.wait())}
        abort_waiter: asyncio.Task[object] | None = None
        if signal is not None:
            abort_waiter = asyncio.create_task(signal.wait())
            waiters.add(abort_waiter)
        done, pending = await asyncio.wait(waiters, timeout=values.timeout, return_when=asyncio.FIRST_COMPLETED)
        timed_out = not done
        aborted = abort_waiter is not None and abort_waiter in done
        if timed_out or aborted:
            process.terminate()
        await process.wait()
        await reader
        for task in pending:
            task.cancel()
        if abort_waiter is not None and abort_waiter not in done:
            abort_waiter.cancel()
        if aborted:
            raise RuntimeError("Operation aborted")
        output = _sanitize_output(b"".join(chunks).decode("utf-8", errors="replace"))
        truncation = truncate_tail(output, max_bytes=DEFAULT_MAX_BYTES)
        if timed_out:
            output = f"{truncation.content}\n\n[Command timed out after {values.timeout:g} seconds]"
        else:
            output = truncation.content
        return AgentToolResult(
            content=[TextContent(text=output)],
            details={
                "exit_code": process.returncode,
                "timed_out": timed_out,
                "total_bytes": total_bytes,
                "truncation": truncation,
            },
        )
