from __future__ import annotations

from typing import Protocol, runtime_checkable

from codeagent.memory.governance_models import (
    CandidateReceipt,
    CandidateStatus,
    MemoryCandidate,
    StageResult,
)
from codeagent.memory.models import (
    MemoryItem,
    MemorySearchHit,
    MemorySearchQuery,
    NewMemoryItem,
)


@runtime_checkable
class MemoryGovernanceRepository(Protocol):
    async def governance_cursor(self, project_id: str, session_id: str) -> int: ...

    async def stage_event_batch(
        self,
        *,
        project_id: str,
        session_id: str,
        expected_ordinal: int,
        next_ordinal: int,
        last_event_id: str | None,
        candidates: tuple[MemoryCandidate, ...],
        receipts: tuple[CandidateReceipt, ...],
    ) -> StageResult: ...

    async def list_pending_candidates(
        self,
        project_id: str,
        session_id: str,
        *,
        limit: int = 100,
    ) -> list[MemoryCandidate]: ...

    async def finalize_candidate(
        self,
        candidate_key: str,
        *,
        outcome: CandidateStatus | str,
        reason: str,
    ) -> None: ...

    async def create(self, draft: NewMemoryItem, *, event_id: str | None = None) -> MemoryItem: ...

    async def search(self, query: MemorySearchQuery) -> list[MemorySearchHit]: ...

    async def supersede_and_create(
        self,
        old_id: str,
        draft: NewMemoryItem,
        *,
        event_id: str | None = None,
    ) -> MemoryItem: ...
