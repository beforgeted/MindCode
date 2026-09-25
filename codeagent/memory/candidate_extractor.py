from __future__ import annotations

from codeagent.evidence.cursor import SequencedEvent
from codeagent.evidence.models import AgentEvent, EventType, EvidenceRef, EvidenceType
from codeagent.memory.governance_models import (
    EXTRACTOR_VERSION,
    MemoryCandidate,
    candidate_key,
    content_hash,
)
from codeagent.memory.models import MemoryScope, MemorySource, MemoryType


class ConservativeCandidateExtractor:
    """保守候选抽取：只认可溯源的消息事件，绝不猜测。

    - USER_MESSAGE → USER_EXPLICIT 候选（跳过 memory_cli 通道，避免 /memory add 回环）。
    - ASSISTANT_MESSAGE → ASSISTANT_DERIVED 候选（交给 Judge 判断，默认不升级）。

    抽取阶段不判断"值不值得记"，那是 MemoryJudge 的职责；这里只保证每个候选
    都带 evidence + source_event_id，可溯源。
    """

    def __init__(self, *, max_candidates: int = 50, include_assistant: bool = True) -> None:
        self.max_candidates = max(1, max_candidates)
        self.include_assistant = include_assistant

    def extract(
        self,
        events: tuple[SequencedEvent, ...],
        *,
        project_id: str,
    ) -> tuple[MemoryCandidate, ...]:
        candidates: list[MemoryCandidate] = []
        for sequenced in events:
            candidate = self._from_event(sequenced.event, project_id=project_id)
            if candidate is not None:
                candidates.append(candidate)
            if len(candidates) >= self.max_candidates:
                break
        return tuple(candidates)

    def _from_event(self, event: AgentEvent, *, project_id: str) -> MemoryCandidate | None:
        if event.type is EventType.USER_MESSAGE:
            if event.payload.get("channel") == "memory_cli":
                return None
            source = MemorySource.USER_EXPLICIT
            reason = "canonical user message"
        elif event.type is EventType.ASSISTANT_MESSAGE and self.include_assistant:
            source = MemorySource.ASSISTANT_DERIVED
            reason = "assistant message"
        else:
            return None

        content = event.payload.get("text")
        if not isinstance(content, str) or not content.strip():
            return None

        event_ids = (event.event_id,)
        evidence = (
            EvidenceRef(
                EvidenceType.MESSAGE,
                event_id=event.event_id,
                session_id=event.session_id,
                agent_run_id=event.agent_run_id,
            ),
        )
        return MemoryCandidate(
            candidate_key=candidate_key(
                project_id=project_id,
                source_event_ids=event_ids,
                source=source,
                proposed_scope=MemoryScope.PROJECT,
                proposed_type=MemoryType.FACT,
                content=content,
            ),
            project_id=project_id,
            session_id=event.session_id,
            content=content,
            content_sha256=content_hash(content),
            source=source,
            evidence_refs=evidence,
            source_event_ids=event_ids,
            reason=reason,
            extractor_version=EXTRACTOR_VERSION,
        )
