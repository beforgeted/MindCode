from __future__ import annotations

from pathlib import Path

from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.memory.governance_models import (
    CandidateReceipt,
    CandidateStatus,
    FilterReason,
    MemoryCandidate,
    candidate_key,
    content_hash,
)
from codeagent.memory.models import MemoryScope, MemorySource, MemoryType
from codeagent.memory.sqlite_store import SqliteMemoryStore


def _candidate(content: str, event_id: str, *, project="p", session="s") -> MemoryCandidate:
    event_ids = (event_id,)
    return MemoryCandidate(
        candidate_key=candidate_key(
            project_id=project,
            source_event_ids=event_ids,
            source=MemorySource.USER_EXPLICIT,
            proposed_scope=MemoryScope.PROJECT,
            proposed_type=MemoryType.FACT,
            content=content,
        ),
        project_id=project,
        session_id=session,
        content=content,
        content_sha256=content_hash(content),
        source=MemorySource.USER_EXPLICIT,
        evidence_refs=(EvidenceRef(EvidenceType.MESSAGE, event_id=event_id, session_id=session),),
        source_event_ids=event_ids,
        reason="test",
    )


async def test_stage_event_batch_advances_cursor_and_dedups(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        assert await store.governance_cursor("p", "s") == 0
        cand = _candidate("项目固定使用 Python 3.11", "evt_1")
        result = await store.stage_event_batch(
            project_id="p",
            session_id="s",
            expected_ordinal=0,
            next_ordinal=1,
            last_event_id="evt_1",
            candidates=(cand,),
            receipts=(),
        )
        assert result.staged == 1
        assert result.duplicates == 0
        assert await store.governance_cursor("p", "s") == 1

        pending = await store.list_pending_candidates("p", "s")
        assert len(pending) == 1
        assert pending[0].content == "项目固定使用 Python 3.11"
        assert pending[0].status is CandidateStatus.PENDING_JUDGE

        # 幂等：同一 candidate_key 再来一次算 duplicate。
        again = await store.stage_event_batch(
            project_id="p",
            session_id="s",
            expected_ordinal=1,
            next_ordinal=2,
            last_event_id="evt_1",
            candidates=(cand,),
            receipts=(),
        )
        assert again.staged == 0
        assert again.duplicates == 1
    finally:
        await store.aclose()


async def test_stage_event_batch_cas_rejects_stale_expected_ordinal(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        first = await store.stage_event_batch(
            project_id="p",
            session_id="s",
            expected_ordinal=0,
            next_ordinal=3,
            last_event_id="evt_3",
            candidates=(),
            receipts=(),
        )
        assert first.next_ordinal == 3
        # 用过期的 expected_ordinal 提交 → CAS 拒绝，游标不动。
        stale = await store.stage_event_batch(
            project_id="p",
            session_id="s",
            expected_ordinal=0,
            next_ordinal=1,
            last_event_id="evt_1",
            candidates=(_candidate("x", "evt_9"),),
            receipts=(),
        )
        assert stale.staged == 0
        assert stale.next_ordinal == 3
        assert await store.governance_cursor("p", "s") == 3
        assert await store.list_pending_candidates("p", "s") == []
    finally:
        await store.aclose()


async def test_receipts_do_not_store_content(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        receipt = CandidateReceipt(
            candidate_key="mcand_secret",
            project_id="p",
            session_id="s",
            content_sha256=content_hash("api_key=supersecret"),
            source_event_ids=("evt_1",),
            outcome=CandidateStatus.FILTERED_SENSITIVE,
            reason=FilterReason.SENSITIVE,
        )
        result = await store.stage_event_batch(
            project_id="p",
            session_id="s",
            expected_ordinal=0,
            next_ordinal=1,
            last_event_id="evt_1",
            candidates=(),
            receipts=(receipt,),
        )
        assert result.receipts == 1
        assert await store.list_pending_candidates("p", "s") == []
    finally:
        await store.aclose()
