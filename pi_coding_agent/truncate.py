"""UTF-8-safe output truncation shared by coding-agent tools."""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MAX_LINES = 2_000
DEFAULT_MAX_BYTES = 50 * 1024
GREP_MAX_LINE_LENGTH = 500


@dataclass(frozen=True)
class TruncationResult:
    content: str
    truncated: bool
    truncated_by: str | None
    total_lines: int
    total_bytes: int
    output_lines: int
    output_bytes: int
    last_line_partial: bool
    first_line_exceeds_limit: bool
    max_lines: int
    max_bytes: int


def _lines(content: str) -> list[str]:
    if not content:
        return []
    result = content.split("\n")
    if content.endswith("\n"):
        result.pop()
    return result


def _tail_utf8(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[-max_bytes:].decode("utf-8", errors="ignore")


def _result(
    content: str,
    *,
    truncated: bool,
    truncated_by: str | None,
    all_lines: list[str],
    total_bytes: int,
    last_line_partial: bool,
    first_line_exceeds_limit: bool,
    max_lines: int,
    max_bytes: int,
) -> TruncationResult:
    return TruncationResult(
        content=content,
        truncated=truncated,
        truncated_by=truncated_by,
        total_lines=len(all_lines),
        total_bytes=total_bytes,
        output_lines=len(_lines(content)),
        output_bytes=len(content.encode("utf-8")),
        last_line_partial=last_line_partial,
        first_line_exceeds_limit=first_line_exceeds_limit,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_head(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep complete leading lines, respecting independent line and byte limits."""
    all_lines = _lines(content)
    total_bytes = len(content.encode("utf-8"))
    if len(all_lines) <= max_lines and total_bytes <= max_bytes:
        return _result(
            content,
            truncated=False,
            truncated_by=None,
            all_lines=all_lines,
            total_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
    if all_lines and len(all_lines[0].encode("utf-8")) > max_bytes:
        return _result(
            "",
            truncated=True,
            truncated_by="bytes",
            all_lines=all_lines,
            total_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=True,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
    kept: list[str] = []
    used = 0
    truncated_by = "lines"
    for index, line in enumerate(all_lines):
        if index >= max_lines:
            break
        added = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + added > max_bytes:
            truncated_by = "bytes"
            break
        kept.append(line)
        used += added
    if len(kept) < len(all_lines) and len(kept) < max_lines:
        truncated_by = "bytes"
    return _result(
        "\n".join(kept),
        truncated=True,
        truncated_by=truncated_by,
        all_lines=all_lines,
        total_bytes=total_bytes,
        last_line_partial=False,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_tail(
    content: str,
    *,
    max_lines: int = DEFAULT_MAX_LINES,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> TruncationResult:
    """Keep the trailing output, allowing one partial first line if necessary."""
    all_lines = _lines(content)
    total_bytes = len(content.encode("utf-8"))
    if len(all_lines) <= max_lines and total_bytes <= max_bytes:
        return _result(
            content,
            truncated=False,
            truncated_by=None,
            all_lines=all_lines,
            total_bytes=total_bytes,
            last_line_partial=False,
            first_line_exceeds_limit=False,
            max_lines=max_lines,
            max_bytes=max_bytes,
        )
    kept: list[str] = []
    used = 0
    partial = False
    truncated_by = "lines"
    for line in reversed(all_lines):
        if len(kept) >= max_lines:
            break
        added = len(line.encode("utf-8")) + (1 if kept else 0)
        if used + added > max_bytes:
            truncated_by = "bytes"
            if not kept:
                kept.append(_tail_utf8(line, max_bytes))
                partial = True
            break
        kept.insert(0, line)
        used += added
    if len(kept) < len(all_lines) and len(kept) < max_lines:
        truncated_by = "bytes"
    return _result(
        "\n".join(kept),
        truncated=True,
        truncated_by=truncated_by,
        all_lines=all_lines,
        total_bytes=total_bytes,
        last_line_partial=partial,
        first_line_exceeds_limit=False,
        max_lines=max_lines,
        max_bytes=max_bytes,
    )


def truncate_line(line: str, max_chars: int = GREP_MAX_LINE_LENGTH) -> tuple[str, bool]:
    if len(line) <= max_chars:
        return line, False
    return f"{line[:max_chars]}... [truncated]", True
