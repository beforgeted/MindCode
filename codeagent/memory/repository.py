from __future__ import annotations

from typing import Protocol, runtime_checkable

from codeagent.memory.models import (
    DeleteResult,
    MemoryItem,
    MemoryListQuery,
    MemorySearchHit,
    MemorySearchQuery,
    NewMemoryItem,
)


@runtime_checkable
class MemoryRepository(Protocol):
    async def start(self) -> None: ...

    async def create(self, draft: NewMemoryItem, *, event_id: str | None = None) -> MemoryItem: ...

    async def get(
        self,
        project_id: str,
        memory_id: str,
        *,
        include_deleted: bool = False,
    ) -> MemoryItem | None: ...

    async def list(self, query: MemoryListQuery) -> list[MemoryItem]: ...

    async def search(self, query: MemorySearchQuery) -> list[MemorySearchHit]: ...

    async def soft_delete(
        self,
        project_id: str,
        memory_id: str,
        *,
        event_id: str | None = None,
    ) -> DeleteResult: ...

    async def revisions(self) -> tuple[int, int]: ...

    async def mark_indexed(self, revision: int) -> None: ...

    async def aclose(self) -> None: ...
