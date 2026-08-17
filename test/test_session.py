"""Tests for the Phase 4.1 session persistence implementation."""

from __future__ import annotations

import json
import uuid
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
    InMemorySessionRepo,
    InMemorySessionStorage,
    JsonlSessionRepo,
    JsonlSessionStorage,
    Session,
    SessionFormatError,
    SessionStorageError,
    uuidv7,
)


def _usage() -> Usage:
    return Usage(
        input=12,
        output=8,
        cache_read=2,
        cache_write=1,
        total_tokens=23,
        cost=CostInfo(input=0.1, output=0.2, cache_read=0.01, cache_write=0.02, total=0.33),
        reasoning=3,
    )


def _messages() -> list[object]:
    return [
        UserMessage(
            content=[TextContent(text="你好"), ImageContent(data="aGVsbG8=", mime_type="image/png")],
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
            usage=_usage(),
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


def test_uuidv7_has_correct_bits_and_is_monotonic() -> None:
    values = [uuidv7() for _ in range(100)]
    parsed = [uuid.UUID(value) for value in values]
    assert all(value.version == 7 for value in parsed)
    assert all(value.variant == uuid.RFC_4122 for value in parsed)
    assert values == sorted(values)
    assert len(set(values)) == len(values)


@pytest.mark.asyncio
async def test_in_memory_session_branches_without_destroying_old_path() -> None:
    storage = await InMemorySessionStorage.create(session_id="memory-1")
    session = Session(storage)
    root = await session.append_message(UserMessage(content="root", timestamp=1))
    old_leaf = await session.append_message(UserMessage(content="old branch", timestamp=2))

    await session.move_to(root)
    new_leaf = await session.append_message(UserMessage(content="new branch", timestamp=3))

    assert await session.get_leaf_id() == new_leaf
    assert [entry.id for entry in await session.get_branch()] == [root, new_leaf]
    assert [entry.id for entry in await session.get_branch(old_leaf)] == [root, old_leaf]
    assert [message.content for message in await session.build_context()] == ["root", "new branch"]

    await session.move_to(None)
    assert await session.build_context() == []


@pytest.mark.asyncio
async def test_jsonl_restart_restores_all_message_types_and_leaf(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    storage = await JsonlSessionStorage.create(path, session_id="persist-1")
    session = Session(storage)
    original = _messages()
    ids = [await session.append_message(message) for message in original]  # type: ignore[arg-type]

    await session.move_to(ids[2])
    restored = Session(await JsonlSessionStorage.open(path))

    assert await restored.get_leaf_id() == ids[2]
    assert await restored.build_context() == original[:3]
    assert await restored.build_context(ids[-1]) == original
    assert (await restored.get_metadata()).id == "persist-1"

    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert records[0]["type"] == "session"
    assert [record["type"] for record in records[1:-1]] == ["entry"] * len(original)
    assert records[-1]["type"] == "leaf"


@pytest.mark.asyncio
async def test_jsonl_branch_survives_second_restart(tmp_path: Path) -> None:
    repo = JsonlSessionRepo(tmp_path)
    session = await repo.create(session_id="tree-1")
    root = await session.append_message(UserMessage(content="root", timestamp=1))
    abandoned = await session.append_message(UserMessage(content="abandoned", timestamp=2))
    await session.move_to(root)
    replacement = await session.append_message(UserMessage(content="replacement", timestamp=3))

    reopened = await repo.open("tree-1")
    assert [entry.id for entry in await reopened.get_branch()] == [root, replacement]
    assert [entry.id for entry in await reopened.get_branch(abandoned)] == [root, abandoned]
    assert [metadata.id for metadata in await repo.list()] == ["tree-1"]


@pytest.mark.asyncio
async def test_repo_validates_ids_duplicates_and_missing_sessions(tmp_path: Path) -> None:
    repo = JsonlSessionRepo(tmp_path)
    await repo.create(session_id="valid.id-1")
    with pytest.raises(SessionStorageError, match="already exists"):
        await repo.create(session_id="valid.id-1")
    with pytest.raises(SessionStorageError, match="session id"):
        await repo.create(session_id="../escape")
    with pytest.raises(SessionStorageError, match="not found"):
        await repo.open("missing")

    memory_repo = InMemorySessionRepo()
    first = await memory_repo.create(session_id="memory")
    assert (await first.get_metadata()).id == "memory"
    assert (await memory_repo.open("memory")).storage is first.storage


@pytest.mark.asyncio
async def test_storage_rejects_invalid_leaf_duplicate_entry_and_non_json_details(tmp_path: Path) -> None:
    storage = await JsonlSessionStorage.create(tmp_path / "errors.jsonl", session_id="errors")
    with pytest.raises(SessionStorageError, match="not found"):
        await storage.set_leaf_id("missing")

    await storage.append_entry(UserMessage(content="hello", timestamp=1), entry_id="entry-1")
    with pytest.raises(SessionStorageError, match="already exists"):
        await storage.append_entry(UserMessage(content="again", timestamp=2), entry_id="entry-1")
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
async def test_open_rejects_corrupt_or_structurally_invalid_jsonl(tmp_path: Path) -> None:
    corrupt = tmp_path / "corrupt.jsonl"
    corrupt.write_text('{"type":"session","version":1,"id":"x","created_at":1}\n{bad', encoding="utf-8")
    with pytest.raises(SessionFormatError, match="invalid JSON"):
        await JsonlSessionStorage.open(corrupt)

    missing_parent = tmp_path / "missing-parent.jsonl"
    missing_parent.write_text(
        '\n'.join(
            [
                '{"type":"session","version":1,"id":"x","created_at":1}',
                '{"type":"entry","id":"child","parent_id":"none","timestamp":2,'
                '"message":{"kind":"user","content":"hi","timestamp":2}}',
            ]
        )
        + '\n',
        encoding="utf-8",
    )
    with pytest.raises(SessionFormatError, match="missing parent"):
        await JsonlSessionStorage.open(missing_parent)
