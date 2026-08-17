"""SQLite-backed session storage and repository.

One database can hold multiple append-only conversation trees. Messages use
the same JSON representation as the JSONL backend, so every AgentMessage type
has identical persistence semantics across both implementations.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pi_agent.messages import AgentMessage

from .repo import SessionRepo, _validate_session_id
from .session import Session
from .storage import (
    SessionEntry,
    SessionFormatError,
    SessionMetadata,
    SessionStorage,
    SessionStorageError,
    _assert_json_value,
    _decode_message,
    _encode_message,
    _now_ms,
    _path_to_root,
)
from .uuid import uuidv7

SQLITE_SCHEMA_VERSION = 1


class SQLiteSessionStorage(SessionStorage):
    """A single session tree stored in a shared SQLite database."""

    def __init__(
        self,
        database: str | Path,
        session_id: str,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> None:
        self.path = Path(database)
        self.session_id = session_id
        self._id_generator = id_generator
        self._lock = asyncio.Lock()

    @classmethod
    async def create(
        cls,
        database: str | Path,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
        id_generator: Callable[[], str] = uuidv7,
    ) -> "SQLiteSessionStorage":
        resolved_id = session_id or id_generator()
        _validate_session_id(resolved_id)
        path = Path(database)
        await asyncio.to_thread(_create_session, path, resolved_id, parent_session_id)
        return cls(path, resolved_id, id_generator=id_generator)

    @classmethod
    async def open(
        cls,
        database: str | Path,
        session_id: str,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> "SQLiteSessionStorage":
        _validate_session_id(session_id)
        path = Path(database)
        await asyncio.to_thread(_require_session, path, session_id)
        return cls(path, session_id, id_generator=id_generator)

    async def get_metadata(self) -> SessionMetadata:
        return await asyncio.to_thread(_read_metadata, self.path, self.session_id)

    async def get_leaf_id(self) -> str | None:
        return await asyncio.to_thread(_read_leaf_id, self.path, self.session_id)

    async def set_leaf_id(self, leaf_id: str | None) -> None:
        async with self._lock:
            await asyncio.to_thread(
                _set_leaf_id,
                self.path,
                self.session_id,
                leaf_id,
                _now_ms(),
            )

    async def append_entry(
        self,
        message: AgentMessage,
        *,
        entry_id: str | None = None,
        timestamp: int | None = None,
    ) -> SessionEntry:
        resolved_id = entry_id or self._id_generator()
        resolved_timestamp = _now_ms() if timestamp is None else timestamp
        encoded_message = _encode_message(message)
        _assert_json_value(encoded_message)
        payload = json.dumps(
            encoded_message,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        async with self._lock:
            return await asyncio.to_thread(
                _append_entry,
                self.path,
                self.session_id,
                resolved_id,
                resolved_timestamp,
                payload,
            )

    async def get_entry(self, entry_id: str) -> SessionEntry | None:
        return await asyncio.to_thread(
            _read_entry,
            self.path,
            self.session_id,
            entry_id,
        )

    async def get_path_to_root(self, entry_id: str | None = None) -> list[SessionEntry]:
        return await asyncio.to_thread(
            _read_path_to_root,
            self.path,
            self.session_id,
            entry_id,
        )


class SQLiteSessionRepo(SessionRepo):
    """Create, open, and list sessions in one local SQLite database."""

    def __init__(
        self,
        database: str | Path,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> None:
        self.database = Path(database)
        self._id_generator = id_generator
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> Session:
        resolved_id = session_id or self._id_generator()
        _validate_session_id(resolved_id)
        async with self._lock:
            storage = await SQLiteSessionStorage.create(
                self.database,
                session_id=resolved_id,
                parent_session_id=parent_session_id,
                id_generator=self._id_generator,
            )
        return Session(storage)

    async def open(self, session: str | SessionMetadata) -> Session:
        session_id = session.id if isinstance(session, SessionMetadata) else session
        storage = await SQLiteSessionStorage.open(
            self.database,
            session_id,
            id_generator=self._id_generator,
        )
        return Session(storage)

    async def list(self) -> list[SessionMetadata]:
        return await asyncio.to_thread(_list_sessions, self.database)


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 30000")
    return connection


def _ensure_schema(connection: sqlite3.Connection) -> None:
    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    if version not in (0, SQLITE_SCHEMA_VERSION):
        raise SessionFormatError(
            f"unsupported SQLite session schema version: {version}"
        )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            parent_session_id TEXT,
            leaf_id TEXT
        );

        CREATE TABLE IF NOT EXISTS session_entries (
            session_id TEXT NOT NULL,
            id TEXT NOT NULL,
            parent_id TEXT,
            timestamp INTEGER NOT NULL,
            message_json TEXT NOT NULL,
            PRIMARY KEY (session_id, id),
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id, parent_id)
                REFERENCES session_entries(session_id, id)
        );

        CREATE INDEX IF NOT EXISTS idx_session_entries_parent
            ON session_entries(session_id, parent_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_updated
            ON sessions(updated_at DESC);
        """
    )
    if version == 0:
        connection.execute(f"PRAGMA user_version = {SQLITE_SCHEMA_VERSION}")
    connection.commit()


def _open_database(path: Path) -> sqlite3.Connection:
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(path)
        _ensure_schema(connection)
        # WAL is persistent and permits readers while another connection writes.
        connection.execute("PRAGMA journal_mode = WAL")
        return connection
    except SessionStorageError:
        if connection is not None:
            connection.close()
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        if connection is not None:
            connection.close()
        raise SessionStorageError(f"failed to open SQLite session database: {path}: {exc}") from exc


def _create_session(path: Path, session_id: str, parent_session_id: str | None) -> None:
    connection = _open_database(path)
    now = _now_ms()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO sessions(id, created_at, updated_at, parent_session_id, leaf_id) "
            "VALUES (?, ?, ?, ?, NULL)",
            (session_id, now, now, parent_session_id),
        )
        connection.commit()
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise SessionStorageError(f"session already exists: {session_id}") from exc
    finally:
        connection.close()


def _require_session(path: Path, session_id: str) -> None:
    connection = _open_database(path)
    try:
        row = connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionStorageError(f"session not found: {session_id}")
    finally:
        connection.close()


def _read_metadata(path: Path, session_id: str) -> SessionMetadata:
    connection = _open_database(path)
    try:
        row = connection.execute(
            "SELECT id, created_at, updated_at, parent_session_id FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionStorageError(f"session not found: {session_id}")
        return _metadata_from_row(row, path)
    finally:
        connection.close()


def _read_leaf_id(path: Path, session_id: str) -> str | None:
    connection = _open_database(path)
    try:
        row = connection.execute(
            "SELECT leaf_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if row is None:
            raise SessionStorageError(f"session not found: {session_id}")
        return row["leaf_id"]
    finally:
        connection.close()


def _set_leaf_id(
    path: Path,
    session_id: str,
    leaf_id: str | None,
    timestamp: int,
) -> None:
    connection = _open_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
        ).fetchone() is None:
            raise SessionStorageError(f"session not found: {session_id}")
        if leaf_id is not None and connection.execute(
            "SELECT 1 FROM session_entries WHERE session_id = ? AND id = ?",
            (session_id, leaf_id),
        ).fetchone() is None:
            raise SessionStorageError(f"entry not found: {leaf_id}")
        connection.execute(
            "UPDATE sessions SET leaf_id = ?, updated_at = MAX(updated_at, ?) WHERE id = ?",
            (leaf_id, timestamp, session_id),
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _append_entry(
    path: Path,
    session_id: str,
    entry_id: str,
    timestamp: int,
    message_json: str,
) -> SessionEntry:
    connection = _open_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        session = connection.execute(
            "SELECT leaf_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise SessionStorageError(f"session not found: {session_id}")
        parent_id = session["leaf_id"]
        connection.execute(
            "INSERT INTO session_entries(session_id, id, parent_id, timestamp, message_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, entry_id, parent_id, timestamp, message_json),
        )
        connection.execute(
            "UPDATE sessions SET leaf_id = ?, updated_at = MAX(updated_at, ?) WHERE id = ?",
            (entry_id, timestamp, session_id),
        )
        connection.commit()
        return SessionEntry(
            id=entry_id,
            parent_id=parent_id,
            timestamp=timestamp,
            message=_decode_message_payload(message_json),
        )
    except sqlite3.IntegrityError as exc:
        connection.rollback()
        raise SessionStorageError(f"entry already exists: {entry_id}") from exc
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _read_entry(path: Path, session_id: str, entry_id: str) -> SessionEntry | None:
    connection = _open_database(path)
    try:
        row = connection.execute(
            "SELECT id, parent_id, timestamp, message_json FROM session_entries "
            "WHERE session_id = ? AND id = ?",
            (session_id, entry_id),
        ).fetchone()
        return _entry_from_row(row) if row is not None else None
    finally:
        connection.close()


def _read_path_to_root(
    path: Path,
    session_id: str,
    entry_id: str | None,
) -> list[SessionEntry]:
    connection = _open_database(path)
    try:
        session = connection.execute(
            "SELECT leaf_id FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise SessionStorageError(f"session not found: {session_id}")
        target = session["leaf_id"] if entry_id is None else entry_id
        rows = connection.execute(
            "SELECT id, parent_id, timestamp, message_json FROM session_entries "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
        entries = {row["id"]: _entry_from_row(row) for row in rows}
        return _path_to_root(entries, target)
    finally:
        connection.close()


def _list_sessions(path: Path) -> list[SessionMetadata]:
    connection = _open_database(path)
    try:
        rows = connection.execute(
            "SELECT id, created_at, updated_at, parent_session_id "
            "FROM sessions ORDER BY updated_at DESC, id ASC"
        ).fetchall()
        return [_metadata_from_row(row, path) for row in rows]
    finally:
        connection.close()


def _metadata_from_row(row: sqlite3.Row, path: Path) -> SessionMetadata:
    return SessionMetadata(
        id=row["id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        parent_session_id=row["parent_session_id"],
        path=str(path.resolve()),
    )


def _entry_from_row(row: sqlite3.Row) -> SessionEntry:
    return SessionEntry(
        id=row["id"],
        parent_id=row["parent_id"],
        timestamp=row["timestamp"],
        message=_decode_message_payload(row["message_json"]),
    )


def _decode_message_payload(payload: str) -> AgentMessage:
    try:
        value: Any = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise SessionFormatError(f"invalid SQLite message JSON: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise SessionFormatError("SQLite message payload must be an object")
    try:
        return _decode_message(value)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SessionFormatError):
            raise
        raise SessionFormatError(f"invalid SQLite message payload: {exc}") from exc


__all__ = ["SQLITE_SCHEMA_VERSION", "SQLiteSessionRepo", "SQLiteSessionStorage"]
