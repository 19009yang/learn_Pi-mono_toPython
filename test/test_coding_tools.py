"""Phase 3.1 minimal tests for the Python coding-agent tools."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from pi_coding_agent.tools import BashTool, EditTool, GlobTool, GrepTool, ReadTool, ToolState, WriteTool


@pytest.mark.asyncio
async def test_bash_runs_echo(tmp_path: Path) -> None:
    tool = BashTool(tmp_path)
    if tool.bash_path is None:
        pytest.skip("bash is unavailable")
    result = await tool.execute("call", {"command": "printf hello"})
    assert result.content[0].text == "hello"


@pytest.mark.asyncio
async def test_read_numbers_lines_and_marks_file_read(tmp_path: Path) -> None:
    target = tmp_path / "sample.py"
    target.write_text("first\nsecond\n", encoding="utf-8")
    state = ToolState()
    result = await ReadTool(tmp_path, state).execute("call", {"path": "sample.py"})
    assert result.content[0].text.startswith("     1\tfirst")
    assert state.was_read(target)


@pytest.mark.asyncio
async def test_write_new_file_then_read_round_trip(tmp_path: Path) -> None:
    state = ToolState()
    writer = WriteTool(tmp_path, state)
    await writer.execute("write", {"path": "nested/output.txt", "content": "hello"})
    result = await ReadTool(tmp_path, state).execute("read", {"path": "nested/output.txt"})
    assert result.content[0].text.endswith("hello")


@pytest.mark.asyncio
async def test_write_and_edit_require_read_before_mutation(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one one", encoding="utf-8")
    state = ToolState()
    writer = WriteTool(tmp_path, state)
    editor = EditTool(tmp_path, state)
    with pytest.raises(PermissionError):
        await writer.execute("write", {"path": "target.txt", "content": "changed"})
    with pytest.raises(PermissionError):
        await editor.execute("edit", {"path": "target.txt", "old_string": "one", "new_string": "two"})


@pytest.mark.asyncio
async def test_edit_requires_unique_match_unless_replace_all(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("one one", encoding="utf-8")
    state = ToolState()
    await ReadTool(tmp_path, state).execute("read", {"path": "target.txt"})
    editor = EditTool(tmp_path, state)
    with pytest.raises(ValueError, match="exactly one"):
        await editor.execute("edit", {"path": "target.txt", "old_string": "one", "new_string": "two"})
    await editor.execute(
        "edit",
        {"path": "target.txt", "old_string": "one", "new_string": "two", "replace_all": True},
    )
    assert target.read_text(encoding="utf-8") == "two two"


@pytest.mark.asyncio
async def test_grep_supports_three_output_modes(tmp_path: Path) -> None:
    (tmp_path / "one.py").write_text("needle\nneedle\n", encoding="utf-8")
    (tmp_path / "two.txt").write_text("needle\n", encoding="utf-8")
    tool = GrepTool(tmp_path)
    content = await tool.execute("grep", {"pattern": "needle"})
    files = await tool.execute("grep", {"pattern": "needle", "output_mode": "files_with_matches"})
    counts = await tool.execute("grep", {"pattern": "needle", "output_mode": "count"})
    assert "one.py:1: needle" in content.content[0].text
    assert {"one.py", "two.txt"} <= set(files.content[0].text.splitlines())
    assert "one.py:2" in counts.content[0].text


@pytest.mark.asyncio
async def test_glob_lists_python_files(tmp_path: Path) -> None:
    (tmp_path / "first.py").write_text("", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "second.py").write_text("", encoding="utf-8")
    result = await GlobTool(tmp_path).execute("glob", {"pattern": "**/*.py"})
    assert {"first.py", "nested/second.py"} <= set(result.content[0].text.splitlines())
