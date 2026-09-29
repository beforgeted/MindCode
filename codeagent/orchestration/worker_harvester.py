"""EventWorkerHarvester（C7）：从一次 Worker 运行的事件里抽取 MemoryCandidate。

复用 `ConservativeCandidateExtractor`（与 Session-End 治理同一套抽取逻辑），作用域限定为该
Worker run 的 `agent_run_id`。Worker 只产候选、不落库；由 `MasterRuntime` 的
`SupervisorMemoryWriter` 在 **promote 成功后**集中 staging（不变式 5）。

注意：Worker 的事件经 `append_nowait` 异步批量落盘，harvest 紧接 run 结束时可能尚未 flush，
所以先 `await event_store.flush()` 再查询，避免漏掉最后一批事件。
"""

from __future__ import annotations

from codeagent.agent.models import AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.evidence.cursor import EventCursor
from codeagent.evidence.event_store import RawEventStore
from codeagent.memory.candidate_extractor import ConservativeCandidateExtractor
from codeagent.memory.governance_models import MemoryCandidate

_PAGE = 500


class EventWorkerHarvester:
    """`WorkerCandidateHarvester` 的默认实现（事件 → 候选）。"""

    def __init__(
        self,
        event_store: RawEventStore,
        project_id: str,
        *,
        extractor: ConservativeCandidateExtractor | None = None,
    ) -> None:
        self._events = event_store
        self._project_id = project_id
        self._extractor = extractor or ConservativeCandidateExtractor()

    async def harvest(
        self, run: AgentRun, result: AgentRunResult
    ) -> tuple[MemoryCandidate, ...]:
        await self._events.flush()  # 确保 Worker 最后一批事件已落盘再读
        mine = []
        cursor = EventCursor(0)
        while True:
            batch = await self._events.query_after(run.session_id, cursor, limit=_PAGE)
            if not batch.events:
                break
            mine.extend(se for se in batch.events if se.event.agent_run_id == run.run_id)
            cursor = batch.next_cursor
            if len(batch.events) < _PAGE:
                break
        # 候选带 EvidenceRef（source_event_ids 指向本 run 的事件），可供 Judge 溯源。
        return self._extractor.extract(tuple(mine), project_id=self._project_id)


__all__ = ["EventWorkerHarvester"]

