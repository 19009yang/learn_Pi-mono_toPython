"""Contract tests for the SQLite session extension backend."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pi_ai.types import (
    AssistantMessage,
    CostInfo,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCall,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from pi_agent.messages import (
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
)
from pi_agent.session import (
    SQLiteSessionRepo,
    SQLiteSessionStorage,
    SessionFormatError,
    SessionStorageError,
)


def _messages() -> list[object]:
    usage = Usage(
        input=12,
        output=8,
        cache_read=2,
        cache_write=1,
        total_tokens=23,
        cost=CostInfo(
            input=0.1,
            output=0.2,
            cache_read=0.01,
            cache_write=0.02,
            total=0.33,
        ),
        reasoning=3,
    )
    return [
        UserMessage(
            content=[
                TextContent(text="你好"),
                ImageContent(data="aGVsbG8=", mime_type="image/png"),
            ],
            timestamp=1,
        ),
        AssistantMessage(
            content=[
                ThinkingContent(thinking="分析", thinking_signature="sig"),
                TextContent(text="答案"),
                ToolCall(id="call-1", name="read", arguments={"path": "README.md"}),
            ],
            api="openai-completions",
            provider="test",
            model="mock",
            usage=usage,
            stop_reason="toolUse",
            timestamp=2,
            response_model="mock-v2",
            response_id="response-1",
        ),
        ToolResultMessage(
            tool_call_id="call-1",
            tool_name="read",
            content=[TextContent(text="file content")],
            is_error=False,
            timestamp=3,
            details={"lines": 1},
        ),
        BashExecutionMessage(
            command="echo hi",
            output="hi",
            exit_code=0,
            cancelled=False,
            truncated=False,
            timestamp=4,
        ),
        CustomMessage(
            custom_type="notice",
            content=[TextContent(text="custom")],
            display=True,
            timestamp=5,
            details={"level": "info"},
        ),
        BranchSummaryMessage(summary="other branch", from_id="old-leaf", timestamp=6),
        CompactionSummaryMessage(summary="earlier history", tokens_before=1000, timestamp=7),
    ]


@pytest.mark.asyncio
async def test_sqlite_restart_restores_all_message_types_and_leaf(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    repo = SQLiteSessionRepo(database)
    session = await repo.create(session_id="persist-1")
    original = _messages()
    ids = [await session.append_message(message) for message in original]  # type: ignore[arg-type]

    await session.move_to(ids[2])
    restored = await SQLiteSessionRepo(database).open("persist-1")

    assert await restored.get_leaf_id() == ids[2]
    assert await restored.build_context() == original[:3]
    assert await restored.build_context(ids[-1]) == original
    metadata = await restored.get_metadata()
    assert metadata.id == "persist-1"
    assert metadata.path == str(database.resolve())


@pytest.mark.asyncio
async def test_sqlite_branches_and_multiple_sessions_survive_restart(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    repo = SQLiteSessionRepo(database)
    tree = await repo.create(session_id="tree-1")
    root = await tree.append_message(UserMessage(content="root", timestamp=1))
    abandoned = await tree.append_message(UserMessage(content="abandoned", timestamp=2))
    await tree.move_to(root)
    replacement = await tree.append_message(UserMessage(content="replacement", timestamp=3))
    child = await repo.create(session_id="child-1", parent_session_id="tree-1")

    reopened_repo = SQLiteSessionRepo(database)
    reopened = await reopened_repo.open("tree-1")
    assert [entry.id for entry in await reopened.get_branch()] == [root, replacement]
    assert [entry.id for entry in await reopened.get_branch(abandoned)] == [root, abandoned]
    assert {metadata.id for metadata in await reopened_repo.list()} == {"tree-1", "child-1"}
    assert (await child.get_metadata()).parent_session_id == "tree-1"


@pytest.mark.asyncio
async def test_sqlite_validates_ids_duplicates_leaf_and_message_details(tmp_path: Path) -> None:
    database = tmp_path / "sessions.db"
    repo = SQLiteSessionRepo(database)
    session = await repo.create(session_id="valid.id-1")
    storage = session.storage
    assert isinstance(storage, SQLiteSessionStorage)

    with pytest.raises(SessionStorageError, match="already exists"):
        await repo.create(session_id="valid.id-1")
    with pytest.raises(SessionStorageError, match="session id"):
        await repo.create(session_id="../escape")
    with pytest.raises(SessionStorageError, match="not found"):
        await repo.open("missing")
    with pytest.raises(SessionStorageError, match="not found"):
        await storage.set_leaf_id("missing")

    await storage.append_entry(
        UserMessage(content="hello", timestamp=1),
        entry_id="entry-1",
    )
    with pytest.raises(SessionStorageError, match="already exists"):
        await storage.append_entry(
            UserMessage(content="again", timestamp=2),
            entry_id="entry-1",
        )
    with pytest.raises(SessionStorageError, match="unsupported type"):
        await storage.append_entry(
            CustomMessage(
                custom_type="bad",
                content="bad details",
                display=False,
                timestamp=3,
                details={"value": object()},
            )
        )


@pytest.mark.asyncio
async def test_sqlite_reports_corrupt_message_and_unknown_schema(tmp_path: Path) -> None:
    corrupt_database = tmp_path / "corrupt.db"
    repo = SQLiteSessionRepo(corrupt_database)
    session = await repo.create(session_id="corrupt-1")
    entry_id = await session.append_message(UserMessage(content="hello", timestamp=1))
    with sqlite3.connect(corrupt_database) as connection:
        connection.execute(
            "UPDATE session_entries SET message_json = ? WHERE session_id = ? AND id = ?",
            ("{bad", "corrupt-1", entry_id),
        )

    with pytest.raises(SessionFormatError, match="invalid SQLite message JSON"):
        await (await SQLiteSessionRepo(corrupt_database).open("corrupt-1")).build_context()

    version_database = tmp_path / "future.db"
    with sqlite3.connect(version_database) as connection:
        connection.execute("PRAGMA user_version = 999")
    with pytest.raises(SessionFormatError, match="unsupported SQLite session schema version"):
        await SQLiteSessionRepo(version_database).list()
