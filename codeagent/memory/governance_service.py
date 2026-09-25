"""P4 记忆治理编排器。

把半成品的几级串成闭环：
    RawEventStore --query_after--> SequencedEvent
        --> ConservativeCandidateExtractor.extract
        --> prefilter（敏感/噪声分流）
        --> stage_event_batch（事务落库 + 游标推进）

保守失败（记忆 V2 §40）：任一环异常 → 记 metric、本批不推进游标（下次重试），
绝不抛异常打断对话，也绝不在证据不足时硬写长期 Memory。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from codeagent.evidence.cursor import EventCursor, SequencedEvent
from codeagent.evidence.event_store import RawEventStore
from codeagent.infra.metrics import Metrics
from codeagent.memory.candidate_extractor import ConservativeCandidateExtractor
from codeagent.memory.conflict import ConflictAction, MemoryConflictResolver
from codeagent.memory.dedup import MemoryDeduplicator
from codeagent.memory.governance_models import CandidateReceipt, MemoryCandidate
from codeagent.memory.governance_repository import MemoryGovernanceRepository
from codeagent.memory.judge import JudgeVerdict, MemoryJudge
from codeagent.memory.models import MemoryItem, MemorySearchQuery, MemoryStatus, NewMemoryItem
from codeagent.memory.prefilter import prefilter


@dataclass(frozen=True, slots=True)
class HarvestReport:
    scanned: int = 0
    staged: int = 0
    receipts: int = 0
    duplicates: int = 0
    next_ordinal: int = 0
    batches: int = 0


@dataclass(frozen=True, slots=True)
class PromoteReport:
    judged: int = 0
    promoted: int = 0
    superseded: int = 0
    skipped: int = 0
    duplicates: int = 0


class MemoryGovernanceService:
    def __init__(
        self,
        *,
        event_store: RawEventStore,
        repository: MemoryGovernanceRepository,
        extractor: ConservativeCandidateExtractor | None = None,
        judge: MemoryJudge | None = None,
        deduplicator: MemoryDeduplicator | None = None,
        conflict_resolver: MemoryConflictResolver | None = None,
        project_id: str,
        batch_limit: int = 200,
        prefilter_max_bytes: int = 16 * 1024,
        promote_limit: int = 100,
        metrics: Metrics | None = None,
    ) -> None:
        self._events = event_store
        self._repo = repository
        self._extractor = extractor or ConservativeCandidateExtractor()
        self._judge = judge
        self._deduper = deduplicator or MemoryDeduplicator()
        self._resolver = conflict_resolver or MemoryConflictResolver()
        self._project_id = project_id
        self._batch_limit = max(1, batch_limit)
        self._prefilter_max_bytes = prefilter_max_bytes
        self._promote_limit = max(1, promote_limit)
        self._metrics = metrics or Metrics()

    async def harvest(self, session_id: str) -> HarvestReport:
        scanned = staged = receipts = duplicates = batches = 0
        last_ordinal = await self._repo.governance_cursor(self._project_id, session_id)
        try:
            while True:
                cursor = await self._repo.governance_cursor(self._project_id, session_id)
                batch = await self._events.query_after(
                    session_id, EventCursor(cursor), limit=self._batch_limit
                )
                if not batch.events:
                    break
                scanned += len(batch.events)
                keep, batch_receipts = self._split(batch.events)
                last_event_id = batch.events[-1].event.event_id
                result = await self._repo.stage_event_batch(
                    project_id=self._project_id,
                    session_id=session_id,
                    expected_ordinal=cursor,
                    next_ordinal=batch.next_cursor.next_ordinal,
                    last_event_id=last_event_id,
                    candidates=keep,
                    receipts=batch_receipts,
                )
                staged += result.staged
                receipts += result.receipts
                duplicates += result.duplicates
                batches += 1
                last_ordinal = result.next_ordinal
                if result.next_ordinal <= cursor:
                    # 游标未推进（CAS 失败或无进展）：避免死循环。
                    break
        except Exception as exc:  # 保守失败：不打断调用方。
            self._metrics.incr("memory.governance.harvest_failures")
            self._metrics.gauge("memory.governance.last_harvest_error", 1.0)
            _ = exc
        self._metrics.incr("memory.governance.staged", staged)
        self._metrics.incr("memory.governance.receipts", receipts)
        return HarvestReport(
            scanned=scanned,
            staged=staged,
            receipts=receipts,
            duplicates=duplicates,
            next_ordinal=last_ordinal,
            batches=batches,
        )

    async def promote(self, session_id: str) -> PromoteReport:
        if self._judge is None:
            return PromoteReport()
        judged = promoted = superseded = skipped = duplicates = 0
        try:
            pending = await self._repo.list_pending_candidates(
                self._project_id, session_id, limit=self._promote_limit
            )
        except Exception:
            self._metrics.incr("memory.governance.promote_failures")
            return PromoteReport()

        for candidate in pending:
            try:
                verdict = await self._judge.judge(candidate)
            except Exception:
                # 保守失败：Judge 异常本轮不写，也不消耗候选（下次重试）。
                self._metrics.incr("memory.governance.judge_failures")
                continue
            judged += 1
            if not verdict.should_remember:
                await self._finalize(candidate, "skipped", "judge_rejected")
                skipped += 1
                continue
            content = verdict.content.strip() or candidate.content
            related = await self._related(content, verdict)
            if self._deduper.find_duplicate(content, related) is not None:
                await self._finalize(candidate, "skipped", "duplicate")
                duplicates += 1
                continue
            decision = self._resolver.resolve(
                content=content,
                source=candidate.source,
                verdict=verdict,
                related=related,
            )
            draft = NewMemoryItem(
                project_id=self._project_id,
                scope=verdict.scope,
                scope_id=self._project_id,
                type=verdict.type,
                content=content,
                source=candidate.source,
                evidence_refs=candidate.evidence_refs,
                confidence=verdict.confidence,
                importance=verdict.importance,
            )
            if decision.action is ConflictAction.SKIP_NEW:
                await self._finalize(candidate, "skipped", decision.reason or "conflict_skip")
                skipped += 1
            elif decision.action is ConflictAction.REQUIRE_USER_CONFIRMATION:
                await self._finalize(candidate, "skipped", "require_user_confirmation")
                skipped += 1
            elif decision.action is ConflictAction.SUPERSEDE_OLD and decision.target_id:
                await self._repo.supersede_and_create(decision.target_id, draft)
                await self._finalize(candidate, "superseded", decision.reason or "supersede")
                superseded += 1
            else:
                await self._repo.create(draft)
                await self._finalize(candidate, "promoted", decision.reason or "keep_both")
                promoted += 1

        self._metrics.incr("memory.governance.promoted", promoted)
        self._metrics.incr("memory.governance.superseded", superseded)
        return PromoteReport(
            judged=judged,
            promoted=promoted,
            superseded=superseded,
            skipped=skipped,
            duplicates=duplicates,
        )

    async def run(self, session_id: str) -> tuple[HarvestReport, PromoteReport]:
        harvest = await self.harvest(session_id)
        promote = await self.promote(session_id)
        return harvest, promote

    async def _related(self, content: str, verdict: JudgeVerdict) -> list[MemoryItem]:
        # 整句做 FTS 短语匹配会失败（尤其中文），改成按关键 term 多次检索取并集，
        # 与 KeywordMemoryRetriever 的思路一致。
        found: dict[str, MemoryItem] = {}
        for term in _search_terms(content, limit=8):
            hits = await self._repo.search(
                MemorySearchQuery(self._project_id, term, type=verdict.type, limit=10)
            )
            for hit in hits:
                if hit.item.status is MemoryStatus.ACTIVE:
                    found[hit.item.id] = hit.item
        return list(found.values())

    async def _finalize(self, candidate: MemoryCandidate, outcome: str, reason: str) -> None:
        try:
            await self._repo.finalize_candidate(
                candidate.candidate_key, outcome=outcome, reason=reason
            )
        except Exception:
            self._metrics.incr("memory.governance.finalize_failures")

    def _split(
        self,
        events: tuple[SequencedEvent, ...],
    ) -> tuple[tuple[MemoryCandidate, ...], tuple[CandidateReceipt, ...]]:
        candidates = self._extractor.extract(events, project_id=self._project_id)
        keep: list[MemoryCandidate] = []
        receipts: list[CandidateReceipt] = []
        for candidate in candidates:
            verdict = prefilter(candidate.content, max_bytes=self._prefilter_max_bytes)
            if verdict.accepted:
                keep.append(candidate)
            else:
                receipts.append(
                    CandidateReceipt(
                        candidate_key=candidate.candidate_key,
                        project_id=candidate.project_id,
                        session_id=candidate.session_id,
                        content_sha256=candidate.content_sha256,
                        source_event_ids=candidate.source_event_ids,
                        outcome=verdict.status,
                        reason=verdict.reason,
                        extractor_version=candidate.extractor_version,
                    )
                )
        return tuple(keep), tuple(receipts)


__all__ = ["HarvestReport", "MemoryGovernanceService", "PromoteReport"]


_LATIN_TERM_RE = re.compile(r"[A-Za-z0-9_.-]{2,}")
_CJK_TERM_RE = re.compile(r"[　-ヿ㐀-鿿豈-﫿]+")


def _search_terms(text: str, *, limit: int) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text)
    terms: list[str] = []

    def _add(term: str) -> None:
        if term and term not in terms and len(terms) < limit:
            terms.append(term)

    for match in _LATIN_TERM_RE.finditer(normalized):
        _add(match.group(0).casefold())
    for match in _CJK_TERM_RE.finditer(normalized):
        run = match.group(0)
        if len(run) <= 3:
            _add(run)
        else:
            for index in range(len(run) - 2):
                _add(run[index : index + 3])
    return terms[:limit]
