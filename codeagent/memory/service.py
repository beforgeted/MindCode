from __future__ import annotations

import unicodedata

from codeagent.evidence.event_store import RawEventStore
from codeagent.evidence.models import AgentEvent, EventType, EvidenceRef, EvidenceType
from codeagent.memory.index_projector import MemoryIndexProjector
from codeagent.memory.models import (
    DeleteResult,
    IndexUpdate,
    MemoryItem,
    MemoryListQuery,
    MemoryScope,
    MemorySearchHit,
    MemorySearchQuery,
    MemorySource,
    MemoryStatus,
    MemoryType,
    MemoryValidationError,
    NewMemoryItem,
)
from codeagent.memory.repository import MemoryRepository


class MemoryService:
    def __init__(
        self,
        repository: MemoryRepository,
        projector: MemoryIndexProjector,
        event_store: RawEventStore,
        *,
        project_id: str,
        session_id: str,
    ) -> None:
        self._repository = repository
        self._projector = projector
        self._events = event_store
        self.project_id = project_id
        self.session_id = session_id
        self.available = False
        self.unavailable_reason: str | None = None

    async def start(self) -> IndexUpdate:
        try:
            await self._repository.start()
            self.available = True
            return await self._projector.refresh_if_stale()
        except Exception as exc:
            self.available = False
            self.unavailable_reason = str(exc)
            return IndexUpdate(False, self.unavailable_reason)

    async def add(
        self,
        content: str,
        memory_type: MemoryType,
        *,
        tags: tuple[str, ...] = (),
    ) -> tuple[MemoryItem, IndexUpdate]:
        self._require_available()
        clean = content.strip()
        if not clean:
            raise MemoryValidationError("Memory 内容不能为空")
        if len(clean.encode("utf-8")) > 64 * 1024:
            raise MemoryValidationError("Memory 内容不能超过 64 KiB")
        clean_tags = _normalize_tags(tags)
        evidence_event = AgentEvent(
            type=EventType.USER_MESSAGE,
            session_id=self.session_id,
            payload={"channel": "memory_cli", "text": clean},
        )
        self._events.append_nowait(evidence_event)
        draft = NewMemoryItem(
            project_id=self.project_id,
            scope=MemoryScope.PROJECT,
            scope_id=self.project_id,
            type=memory_type,
            content=clean,
            source=MemorySource.USER_EXPLICIT,
            tags=clean_tags,
            evidence_refs=(
                EvidenceRef(
                    EvidenceType.MESSAGE,
                    event_id=evidence_event.event_id,
                    session_id=self.session_id,
                ),
            ),
        )
        item = await self._repository.create(draft, event_id=evidence_event.event_id)
        self._events.append_nowait(
            AgentEvent(
                type=EventType.MEMORY_CREATED,
                session_id=self.session_id,
                payload={"memory_id": item.id, "project_id": self.project_id},
            )
        )
        return item, await self._projector.refresh_if_stale()

    async def list(
        self,
        *,
        memory_type: MemoryType | None = None,
        status: MemoryStatus = MemoryStatus.ACTIVE,
        limit: int = 20,
    ) -> list[MemoryItem]:
        self._require_available()
        return await self._repository.list(
            MemoryListQuery(self.project_id, type=memory_type, status=status, limit=limit)
        )

    async def search(
        self,
        text: str,
        *,
        memory_type: MemoryType | None = None,
        limit: int = 20,
    ) -> list[MemorySearchHit]:
        self._require_available()
        return await self._repository.search(
            MemorySearchQuery(self.project_id, text, type=memory_type, limit=limit)
        )

    async def show(self, memory_id: str) -> MemoryItem | None:
        self._require_available()
        return await self._repository.get(self.project_id, memory_id, include_deleted=True)

    async def delete(self, memory_id: str) -> tuple[DeleteResult, IndexUpdate]:
        self._require_available()
        result = await self._repository.soft_delete(self.project_id, memory_id)
        if not result.already_deleted:
            self._events.append_nowait(
                AgentEvent(
                    type=EventType.MEMORY_UPDATED,
                    session_id=self.session_id,
                    payload={
                        "memory_id": result.item.id,
                        "project_id": self.project_id,
                        "status": str(MemoryStatus.DELETED),
                    },
                )
            )
        return result, await self._projector.refresh_if_stale()

    async def aclose(self) -> None:
        await self._repository.aclose()

    def _require_available(self) -> None:
        if not self.available:
            from codeagent.memory.models import MemoryUnavailableError

            raise MemoryUnavailableError(self.unavailable_reason or "Memory 不可用")


def _normalize_tags(tags: tuple[str, ...]) -> tuple[str, ...]:
    if len(tags) > 20:
        raise MemoryValidationError("最多允许 20 个 tags")
    out: list[str] = []
    for tag in tags:
        clean = unicodedata.normalize("NFKC", tag).strip().casefold()
        if not clean:
            continue
        if len(clean) > 64:
            raise MemoryValidationError("单个 tag 不能超过 64 字符")
        if clean not in out:
            out.append(clean)
    return tuple(out)
