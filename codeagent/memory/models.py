from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from codeagent.evidence.models import EvidenceRef


class MemoryScope(StrEnum):
    RUN = "run"
    SESSION = "session"
    USER = "user"
    PROJECT = "project"
    AGENT = "agent"


class MemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    CONSTRAINT = "constraint"
    DECISION = "decision"
    FAILURE = "failure"
    WORKFLOW = "workflow"
    TOOL_INSIGHT = "tool_insight"
    REFERENCE = "reference"


class MemorySource(StrEnum):
    USER_EXPLICIT = "user_explicit"
    TOOL_VERIFIED = "tool_verified"
    ASSISTANT_DERIVED = "assistant_derived"


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    DELETED = "deleted"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class MemoryError(RuntimeError):
    pass


class MemoryUnavailableError(MemoryError):
    pass


class MemoryNotFoundError(MemoryError):
    pass


class MemoryValidationError(MemoryError):
    pass


@dataclass(frozen=True, slots=True)
class NewMemoryItem:
    project_id: str
    scope: MemoryScope
    scope_id: str
    type: MemoryType
    content: str
    source: MemorySource
    evidence_refs: tuple[EvidenceRef, ...] = ()
    tags: tuple[str, ...] = ()
    confidence: float | None = None
    importance: int | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryItem:
    id: str
    project_id: str
    scope: MemoryScope
    scope_id: str
    type: MemoryType
    content: str
    source: MemorySource
    status: MemoryStatus
    evidence_refs: tuple[EvidenceRef, ...]
    tags: tuple[str, ...]
    confidence: float | None
    importance: int | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime | None = None
    deleted_at: datetime | None = None
    version: int = 1

    @property
    def active(self) -> bool:
        if self.status is not MemoryStatus.ACTIVE:
            return False
        return self.expires_at is None or self.expires_at > datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class MemoryListQuery:
    project_id: str
    type: MemoryType | None = None
    status: MemoryStatus = MemoryStatus.ACTIVE
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True, slots=True)
class MemorySearchQuery:
    project_id: str
    text: str
    type: MemoryType | None = None
    limit: int = 20


@dataclass(frozen=True, slots=True)
class MemorySearchHit:
    item: MemoryItem
    score: float


@dataclass(frozen=True, slots=True)
class DeleteResult:
    item: MemoryItem
    already_deleted: bool = False


@dataclass(frozen=True, slots=True)
class IndexUpdate:
    ok: bool
    warning: str | None = None
