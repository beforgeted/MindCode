from __future__ import annotations

import hashlib
import json
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from codeagent.evidence.models import EvidenceRef
from codeagent.memory.models import MemoryScope, MemorySource, MemoryType

CONTRACT_VERSION = 1
EXTRACTOR_VERSION = "user-message-v1"


class CandidateStatus(StrEnum):
    PENDING_JUDGE = "pending_judge"
    FILTERED_SENSITIVE = "filtered_sensitive"
    FILTERED_NOISE = "filtered_noise"


class FilterReason(StrEnum):
    ACCEPTED = "accepted"
    EMPTY = "empty"
    NOISE = "noise"
    SENSITIVE = "sensitive"
    TOO_LARGE = "too_large"


class MemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    candidate_key: str
    project_id: str
    session_id: str
    content: str = Field(min_length=1)
    content_sha256: str
    source: MemorySource
    proposed_scope: MemoryScope = MemoryScope.PROJECT
    proposed_type: MemoryType = MemoryType.FACT
    evidence_refs: tuple[EvidenceRef, ...]
    source_event_ids: tuple[str, ...]
    reason: str
    extractor_version: str = EXTRACTOR_VERSION
    contract_version: int = CONTRACT_VERSION
    status: CandidateStatus = CandidateStatus.PENDING_JUDGE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def validate_provenance(self) -> MemoryCandidate:
        if not self.evidence_refs or not self.source_event_ids:
            raise ValueError("Memory candidate 必须包含 evidence 和 source event")
        if self.content_sha256 != content_hash(self.content):
            raise ValueError("content_sha256 与正文不一致")
        return self


class CandidateReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_key: str
    project_id: str
    session_id: str
    content_sha256: str
    source_event_ids: tuple[str, ...]
    outcome: CandidateStatus
    reason: FilterReason
    extractor_version: str = EXTRACTOR_VERSION
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StageResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    staged: int = 0
    receipts: int = 0
    duplicates: int = 0
    next_ordinal: int = 0


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def candidate_key(
    *,
    project_id: str,
    source_event_ids: tuple[str, ...],
    source: MemorySource,
    proposed_scope: MemoryScope,
    proposed_type: MemoryType,
    content: str,
    extractor_version: str = EXTRACTOR_VERSION,
) -> str:
    fingerprint = " ".join(unicodedata.normalize("NFKC", content).split())
    payload = {
        "contract_version": CONTRACT_VERSION,
        "project_id": project_id,
        "extractor_version": extractor_version,
        "source_event_ids": source_event_ids,
        "source": str(source),
        "scope": str(proposed_scope),
        "type": str(proposed_type),
        "content": fingerprint,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "mcand_" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
