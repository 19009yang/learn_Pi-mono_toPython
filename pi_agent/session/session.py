"""High-level session tree operations."""

from __future__ import annotations

from copy import deepcopy

from pi_agent.messages import AgentMessage

from .storage import SessionEntry, SessionMetadata, SessionStorage


class Session:
    """A conversation tree backed by a :class:`SessionStorage` implementation."""

    def __init__(self, storage: SessionStorage) -> None:
        self.storage = storage

    async def get_metadata(self) -> SessionMetadata:
        return await self.storage.get_metadata()

    async def get_leaf_id(self) -> str | None:
        return await self.storage.get_leaf_id()

    async def get_branch(self, entry_id: str | None = None) -> list[SessionEntry]:
        """Return the selected branch in chronological (root-to-leaf) order."""

        return await self.storage.get_path_to_root(entry_id)

    async def build_context(self, entry_id: str | None = None) -> list[AgentMessage]:
        """Rebuild the messages visible at a branch leaf."""

        return [deepcopy(entry.message) for entry in await self.get_branch(entry_id)]

    async def append_message(self, message: AgentMessage) -> str:
        entry = await self.storage.append_entry(message)
        return entry.id

    async def move_to(self, entry_id: str | None) -> None:
        """Move the leaf pointer without deleting the branch being left behind."""

        await self.storage.set_leaf_id(entry_id)
