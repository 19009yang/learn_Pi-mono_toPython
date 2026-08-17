"""Storage backends for append-only, branching agent sessions."""

from __future__ import annotations

import asyncio
import json
import math
import os
import time
from abc import ABC, abstractmethod
from copy import deepcopy
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any, Callable

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
    AgentMessage,
    BashExecutionMessage,
    BranchSummaryMessage,
    CompactionSummaryMessage,
    CustomMessage,
)

from .uuid import uuidv7

JSONL_VERSION = 1


class SessionStorageError(RuntimeError):
    """Base error for invalid session state or durable data."""


class SessionFormatError(SessionStorageError):
    """Raised when a JSONL session cannot be decoded safely."""


@dataclass(frozen=True)
class SessionMetadata:
    id: str
    created_at: int
    updated_at: int
    parent_session_id: str | None = None
    path: str | None = None


@dataclass(frozen=True)
class SessionEntry:
    id: str
    parent_id: str | None
    timestamp: int
    message: AgentMessage


class SessionStorage(ABC):
    """Abstract persistence boundary used by :class:`Session`."""

    @abstractmethod
    async def get_metadata(self) -> SessionMetadata:
        raise NotImplementedError

    @abstractmethod
    async def get_leaf_id(self) -> str | None:
        raise NotImplementedError

    @abstractmethod
    async def set_leaf_id(self, leaf_id: str | None) -> None:
        raise NotImplementedError

    @abstractmethod
    async def append_entry(
        self,
        message: AgentMessage,
        *,
        entry_id: str | None = None,
        timestamp: int | None = None,
    ) -> SessionEntry:
        raise NotImplementedError

    @abstractmethod
    async def get_entry(self, entry_id: str) -> SessionEntry | None:
        raise NotImplementedError

    @abstractmethod
    async def get_path_to_root(self, entry_id: str | None = None) -> list[SessionEntry]:
        """Return entries from root to ``entry_id`` (or the current leaf)."""

        raise NotImplementedError


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class InMemorySessionStorage(SessionStorage):
    """Session storage suitable for tests and ephemeral conversations."""

    def __init__(
        self,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        created_at: int | None = None,
        id_generator: Callable[[], str] = uuidv7,
    ) -> None:
        now = _now_ms() if created_at is None else created_at
        self._metadata = SessionMetadata(
            id=session_id or id_generator(),
            created_at=now,
            updated_at=now,
            parent_session_id=parent_session_id,
        )
        self._entries: dict[str, SessionEntry] = {}
        self._leaf_id: str | None = None
        self._id_generator = id_generator
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        id_generator: Callable[[], str] = uuidv7,
    ) -> "InMemorySessionStorage":
        return cls(
            session_id=session_id,
            parent_session_id=parent_session_id,
            id_generator=id_generator,
        )

    async def get_metadata(self) -> SessionMetadata:
        return deepcopy(self._metadata)

    async def get_leaf_id(self) -> str | None:
        return self._leaf_id

    async def set_leaf_id(self, leaf_id: str | None) -> None:
        async with self._lock:
            self._validate_leaf(leaf_id)
            self._leaf_id = leaf_id
            self._touch()

    async def append_entry(
        self,
        message: AgentMessage,
        *,
        entry_id: str | None = None,
        timestamp: int | None = None,
    ) -> SessionEntry:
        async with self._lock:
            resolved_id = entry_id or self._id_generator()
            if resolved_id in self._entries:
                raise SessionStorageError(f"entry already exists: {resolved_id}")
            entry = SessionEntry(
                id=resolved_id,
                parent_id=self._leaf_id,
                timestamp=_now_ms() if timestamp is None else timestamp,
                message=deepcopy(message),
            )
            _assert_json_value(_encode_entry(entry))
            self._entries[resolved_id] = entry
            self._leaf_id = resolved_id
            self._touch(entry.timestamp)
            return deepcopy(entry)

    async def get_entry(self, entry_id: str) -> SessionEntry | None:
        entry = self._entries.get(entry_id)
        return deepcopy(entry) if entry is not None else None

    async def get_path_to_root(self, entry_id: str | None = None) -> list[SessionEntry]:
        target = self._leaf_id if entry_id is None else entry_id
        return deepcopy(_path_to_root(self._entries, target))

    def _validate_leaf(self, leaf_id: str | None) -> None:
        if leaf_id is not None and leaf_id not in self._entries:
            raise SessionStorageError(f"entry not found: {leaf_id}")

    def _touch(self, timestamp: int | None = None) -> None:
        updated_at = max(self._metadata.updated_at, timestamp or _now_ms())
        self._metadata = SessionMetadata(
            id=self._metadata.id,
            created_at=self._metadata.created_at,
            updated_at=updated_at,
            parent_session_id=self._metadata.parent_session_id,
            path=self._metadata.path,
        )


class JsonlSessionStorage(SessionStorage):
    """Append-only JSONL storage with in-memory indexes rebuilt on open."""

    def __init__(
        self,
        path: str | Path,
        metadata: SessionMetadata,
        entries: dict[str, SessionEntry],
        leaf_id: str | None,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> None:
        self.path = Path(path)
        self._metadata = metadata
        self._entries = entries
        self._leaf_id = leaf_id
        self._id_generator = id_generator
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        path: str | Path,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        id_generator: Callable[[], str] = uuidv7,
    ) -> "JsonlSessionStorage":
        resolved = Path(path)
        now = _now_ms()
        metadata = SessionMetadata(
            id=session_id or id_generator(),
            created_at=now,
            updated_at=now,
            parent_session_id=parent_session_id,
            path=str(resolved.resolve()),
        )
        header = {
            "type": "session",
            "version": JSONL_VERSION,
            "id": metadata.id,
            "created_at": metadata.created_at,
            "parent_session_id": metadata.parent_session_id,
        }
        await asyncio.to_thread(_create_jsonl_file, resolved, header)
        return cls(resolved, metadata, {}, None, id_generator=id_generator)

    @classmethod
    async def open(
        cls,
        path: str | Path,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> "JsonlSessionStorage":
        resolved = Path(path)
        records = await asyncio.to_thread(_read_jsonl_file, resolved)
        if not records:
            raise SessionFormatError(f"empty session file: {resolved}")
        header = records[0]
        if header.get("type") != "session" or header.get("version") != JSONL_VERSION:
            raise SessionFormatError(f"unsupported session header: {resolved}")
        if not isinstance(header.get("id"), str) or not isinstance(header.get("created_at"), int):
            raise SessionFormatError(f"invalid session header: {resolved}")

        entries: dict[str, SessionEntry] = {}
        leaf_id: str | None = None
        updated_at = header["created_at"]
        for line_number, record in enumerate(records[1:], start=2):
            try:
                record_type = record.get("type")
                if record_type == "entry":
                    entry = _decode_entry(record)
                    if entry.id in entries:
                        raise SessionFormatError(f"duplicate entry id: {entry.id}")
                    if entry.parent_id is not None and entry.parent_id not in entries:
                        raise SessionFormatError(f"missing parent entry: {entry.parent_id}")
                    entries[entry.id] = entry
                    leaf_id = entry.id
                    updated_at = max(updated_at, entry.timestamp)
                elif record_type == "leaf":
                    candidate = record.get("leaf_id")
                    if candidate is not None and not isinstance(candidate, str):
                        raise SessionFormatError("leaf_id must be a string or null")
                    if candidate is not None and candidate not in entries:
                        raise SessionFormatError(f"leaf entry not found: {candidate}")
                    leaf_id = candidate
                    timestamp = record.get("timestamp")
                    if not isinstance(timestamp, int):
                        raise SessionFormatError("leaf timestamp must be an integer")
                    updated_at = max(updated_at, timestamp)
                else:
                    raise SessionFormatError(f"unknown record type: {record_type!r}")
            except (KeyError, TypeError, ValueError, SessionStorageError) as exc:
                if isinstance(exc, SessionFormatError):
                    detail = str(exc)
                else:
                    detail = f"{type(exc).__name__}: {exc}"
                raise SessionFormatError(f"{resolved}:{line_number}: {detail}") from exc

        metadata = SessionMetadata(
            id=header["id"],
            created_at=header["created_at"],
            updated_at=updated_at,
            parent_session_id=header.get("parent_session_id"),
            path=str(resolved.resolve()),
        )
        return cls(resolved, metadata, entries, leaf_id, id_generator=id_generator)

    async def get_metadata(self) -> SessionMetadata:
        return deepcopy(self._metadata)

    async def get_leaf_id(self) -> str | None:
        return self._leaf_id

    async def set_leaf_id(self, leaf_id: str | None) -> None:
        async with self._lock:
            if leaf_id is not None and leaf_id not in self._entries:
                raise SessionStorageError(f"entry not found: {leaf_id}")
            timestamp = _now_ms()
            await asyncio.to_thread(
                _append_jsonl_record,
                self.path,
                {"type": "leaf", "leaf_id": leaf_id, "timestamp": timestamp},
            )
            self._leaf_id = leaf_id
            self._touch(timestamp)

    async def append_entry(
        self,
        message: AgentMessage,
        *,
        entry_id: str | None = None,
        timestamp: int | None = None,
    ) -> SessionEntry:
        async with self._lock:
            resolved_id = entry_id or self._id_generator()
            if resolved_id in self._entries:
                raise SessionStorageError(f"entry already exists: {resolved_id}")
            entry = SessionEntry(
                id=resolved_id,
                parent_id=self._leaf_id,
                timestamp=_now_ms() if timestamp is None else timestamp,
                message=deepcopy(message),
            )
            record = _encode_entry(entry)
            _assert_json_value(record)
            await asyncio.to_thread(_append_jsonl_record, self.path, record)
            self._entries[resolved_id] = entry
            self._leaf_id = resolved_id
            self._touch(entry.timestamp)
            return deepcopy(entry)

    async def get_entry(self, entry_id: str) -> SessionEntry | None:
        entry = self._entries.get(entry_id)
        return deepcopy(entry) if entry is not None else None

    async def get_path_to_root(self, entry_id: str | None = None) -> list[SessionEntry]:
        target = self._leaf_id if entry_id is None else entry_id
        return deepcopy(_path_to_root(self._entries, target))

    def _touch(self, timestamp: int) -> None:
        self._metadata = SessionMetadata(
            id=self._metadata.id,
            created_at=self._metadata.created_at,
            updated_at=max(self._metadata.updated_at, timestamp),
            parent_session_id=self._metadata.parent_session_id,
            path=self._metadata.path,
        )


def _path_to_root(
    entries: dict[str, SessionEntry], entry_id: str | None
) -> list[SessionEntry]:
    if entry_id is None:
        return []
    path: list[SessionEntry] = []
    seen: set[str] = set()
    current_id: str | None = entry_id
    while current_id is not None:
        if current_id in seen:
            raise SessionStorageError(f"cycle detected at entry: {current_id}")
        seen.add(current_id)
        entry = entries.get(current_id)
        if entry is None:
            raise SessionStorageError(f"entry not found: {current_id}")
        path.append(entry)
        current_id = entry.parent_id
    path.reverse()
    return path


def _create_jsonl_file(path: Path, header: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(_json_line(header))
        stream.flush()
        os.fsync(stream.fileno())


def _append_jsonl_record(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(_json_line(record))
        stream.flush()
        os.fsync(stream.fileno())


def _read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise SessionFormatError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
                if not isinstance(value, dict):
                    raise SessionFormatError(f"{path}:{line_number}: record must be an object")
                records.append(value)
    except FileNotFoundError as exc:
        raise SessionStorageError(f"session file not found: {path}") from exc
    return records


def _json_line(value: dict[str, Any]) -> str:
    _assert_json_value(value)
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n"


def _assert_json_value(value: Any, *, _seen: set[int] | None = None) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SessionStorageError("payload contains a non-finite number")
        return
    seen = set() if _seen is None else _seen
    if isinstance(value, (list, dict)):
        identity = id(value)
        if identity in seen:
            raise SessionStorageError("payload contains a cycle")
        seen.add(identity)
        try:
            values = value if isinstance(value, list) else value.values()
            if isinstance(value, dict) and not all(isinstance(key, str) for key in value):
                raise SessionStorageError("payload contains a non-string object key")
            for item in values:
                _assert_json_value(item, _seen=seen)
        finally:
            seen.remove(identity)
        return
    raise SessionStorageError(f"payload contains unsupported type: {type(value).__name__}")


def _encode_entry(entry: SessionEntry) -> dict[str, Any]:
    return {
        "type": "entry",
        "id": entry.id,
        "parent_id": entry.parent_id,
        "timestamp": entry.timestamp,
        "message": _encode_message(entry.message),
    }


def _decode_entry(record: dict[str, Any]) -> SessionEntry:
    entry_id = record["id"]
    parent_id = record.get("parent_id")
    timestamp = record["timestamp"]
    if not isinstance(entry_id, str) or not isinstance(timestamp, int):
        raise SessionFormatError("entry id and timestamp have invalid types")
    if parent_id is not None and not isinstance(parent_id, str):
        raise SessionFormatError("entry parent_id must be a string or null")
    message = record["message"]
    if not isinstance(message, dict):
        raise SessionFormatError("entry message must be an object")
    return SessionEntry(entry_id, parent_id, timestamp, _decode_message(message))


def _plain_dataclass(value: Any) -> dict[str, Any]:
    if not is_dataclass(value):
        raise SessionStorageError(f"expected dataclass, got {type(value).__name__}")
    return asdict(value)


def _encode_content(value: Any) -> dict[str, Any]:
    if isinstance(value, TextContent):
        return {"kind": "text", **_plain_dataclass(value)}
    if isinstance(value, ImageContent):
        return {"kind": "image", **_plain_dataclass(value)}
    if isinstance(value, ThinkingContent):
        return {"kind": "thinking", **_plain_dataclass(value)}
    if isinstance(value, ToolCall):
        return {"kind": "tool_call", **_plain_dataclass(value)}
    raise SessionStorageError(f"unsupported content type: {type(value).__name__}")


def _decode_content(value: dict[str, Any]) -> Any:
    kind = value.get("kind")
    data = {key: item for key, item in value.items() if key not in {"kind", "type"}}
    if kind == "text":
        return TextContent(**data)
    if kind == "image":
        return ImageContent(**data)
    if kind == "thinking":
        return ThinkingContent(**data)
    if kind == "tool_call":
        return ToolCall(**data)
    raise SessionFormatError(f"unsupported content kind: {kind!r}")


def _encode_message(message: AgentMessage) -> dict[str, Any]:
    if isinstance(message, UserMessage):
        content = message.content
        encoded_content = content if isinstance(content, str) else [_encode_content(item) for item in content]
        return {"kind": "user", "content": encoded_content, "timestamp": message.timestamp}
    if isinstance(message, AssistantMessage):
        return {
            "kind": "assistant",
            "content": [_encode_content(item) for item in message.content],
            "api": message.api,
            "provider": message.provider,
            "model": message.model,
            "usage": _plain_dataclass(message.usage),
            "stop_reason": message.stop_reason,
            "timestamp": message.timestamp,
            "response_model": message.response_model,
            "response_id": message.response_id,
            "error_message": message.error_message,
        }
    if isinstance(message, ToolResultMessage):
        return {
            "kind": "tool_result",
            "tool_call_id": message.tool_call_id,
            "tool_name": message.tool_name,
            "content": [_encode_content(item) for item in message.content],
            "is_error": message.is_error,
            "timestamp": message.timestamp,
            "details": message.details,
        }
    if isinstance(message, BashExecutionMessage):
        return {"kind": "bash_execution", **_plain_dataclass(message)}
    if isinstance(message, CustomMessage):
        data = _plain_dataclass(message)
        data["content"] = (
            message.content
            if isinstance(message.content, str)
            else [_encode_content(item) for item in message.content]
        )
        return {"kind": "custom", **data}
    if isinstance(message, BranchSummaryMessage):
        return {"kind": "branch_summary", **_plain_dataclass(message)}
    if isinstance(message, CompactionSummaryMessage):
        return {"kind": "compaction_summary", **_plain_dataclass(message)}
    raise SessionStorageError(f"unsupported message type: {type(message).__name__}")


def _decode_message(value: dict[str, Any]) -> AgentMessage:
    kind = value.get("kind")
    data = {key: item for key, item in value.items() if key not in {"kind", "role"}}
    if kind == "user":
        content = data["content"]
        if isinstance(content, list):
            content = [_decode_content(item) for item in content]
        return UserMessage(content=content, timestamp=data["timestamp"])
    if kind == "assistant":
        usage_data = data.pop("usage")
        cost_data = usage_data.pop("cost")
        usage = Usage(cost=CostInfo(**cost_data), **usage_data)
        data["content"] = [_decode_content(item) for item in data["content"]]
        return AssistantMessage(usage=usage, **data)
    if kind == "tool_result":
        data["content"] = [_decode_content(item) for item in data["content"]]
        return ToolResultMessage(**data)
    if kind == "bash_execution":
        return BashExecutionMessage(**data)
    if kind == "custom":
        content = data["content"]
        if isinstance(content, list):
            data["content"] = [_decode_content(item) for item in content]
        return CustomMessage(**data)
    if kind == "branch_summary":
        return BranchSummaryMessage(**data)
    if kind == "compaction_summary":
        return CompactionSummaryMessage(**data)
    raise SessionFormatError(f"unsupported message kind: {kind!r}")
