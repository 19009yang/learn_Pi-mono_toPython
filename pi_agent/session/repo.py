"""Repositories that create, discover, and open sessions."""

from __future__ import annotations

import asyncio
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

from .session import Session
from .storage import (
    InMemorySessionStorage,
    JsonlSessionStorage,
    SessionMetadata,
    SessionStorageError,
)
from .uuid import uuidv7

_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")


def _validate_session_id(session_id: str) -> None:
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionStorageError(
            "session id must contain only letters, numbers, '.', '_', or '-', "
            "and must start and end with a letter or number"
        )


class SessionRepo(ABC):
    @abstractmethod
    async def create(
        self,
        *,
        session_id: str | None = None,
        parent_session_id: str | None = None,
    ) -> Session:
        raise NotImplementedError

    @abstractmethod
    async def open(self, session: str | SessionMetadata) -> Session:
        raise NotImplementedError

    @abstractmethod
    async def list(self) -> list[SessionMetadata]:
        raise NotImplementedError


class InMemorySessionRepo(SessionRepo):
    def __init__(self, *, id_generator: Callable[[], str] = uuidv7) -> None:
        self._storages: dict[str, InMemorySessionStorage] = {}
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
            if resolved_id in self._storages:
                raise SessionStorageError(f"session already exists: {resolved_id}")
            storage = await InMemorySessionStorage.create(
                session_id=resolved_id,
                parent_session_id=parent_session_id,
                id_generator=self._id_generator,
            )
            self._storages[resolved_id] = storage
        return Session(storage)

    async def open(self, session: str | SessionMetadata) -> Session:
        session_id = session.id if isinstance(session, SessionMetadata) else session
        storage = self._storages.get(session_id)
        if storage is None:
            raise SessionStorageError(f"session not found: {session_id}")
        return Session(storage)

    async def list(self) -> list[SessionMetadata]:
        metadata = [await storage.get_metadata() for storage in self._storages.values()]
        return sorted(metadata, key=lambda item: item.updated_at, reverse=True)


class JsonlSessionRepo(SessionRepo):
    def __init__(
        self,
        sessions_root: str | Path,
        *,
        id_generator: Callable[[], str] = uuidv7,
    ) -> None:
        self.sessions_root = Path(sessions_root)
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
        path = self._path_for(resolved_id)
        async with self._lock:
            try:
                storage = await JsonlSessionStorage.create(
                    path,
                    session_id=resolved_id,
                    parent_session_id=parent_session_id,
                    id_generator=self._id_generator,
                )
            except FileExistsError as exc:
                raise SessionStorageError(f"session already exists: {resolved_id}") from exc
        return Session(storage)

    async def open(self, session: str | SessionMetadata) -> Session:
        if isinstance(session, SessionMetadata):
            path = Path(session.path) if session.path else self._path_for(session.id)
            expected_id = session.id
        else:
            _validate_session_id(session)
            path = self._path_for(session)
            expected_id = session
        storage = await JsonlSessionStorage.open(path, id_generator=self._id_generator)
        metadata = await storage.get_metadata()
        if metadata.id != expected_id:
            raise SessionStorageError(
                f"session id does not match file header: expected {expected_id}, got {metadata.id}"
            )
        return Session(storage)

    async def list(self) -> list[SessionMetadata]:
        paths = await asyncio.to_thread(self._session_paths)
        metadata: list[SessionMetadata] = []
        for path in paths:
            storage = await JsonlSessionStorage.open(path, id_generator=self._id_generator)
            metadata.append(await storage.get_metadata())
        return sorted(metadata, key=lambda item: item.updated_at, reverse=True)

    def _path_for(self, session_id: str) -> Path:
        return self.sessions_root / f"{session_id}.jsonl"

    def _session_paths(self) -> list[Path]:
        if not self.sessions_root.exists():
            return []
        return sorted(self.sessions_root.glob("*.jsonl"))
