"""Multi-Agent 共享 Memory：Worker 产候选，Supervisor 集中写（P6）。

不变式（记忆 V2 §47.7 / §29）：**Worker 绝不直写 PROJECT Memory**。Worker 只在
`AgentRunResult.memory_candidates` 里产出 `MemoryCandidate`；由 Master（Supervisor）
汇总、去重、按产出 Agent 的 `MemoryProfile.writable_types` 过滤越界候选，集中 staging
到候选表（PENDING_JUDGE）。最终是否落长期 Memory，仍由既有 LLM Judge 治理链
（Session-End `governance.run()` 或 `/memory harvest`）裁决——Supervisor 不自行 promote。

与 Session-End harvest 走同一 candidate_key + `INSERT OR IGNORE`，天然去重不双写。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codeagent.infra.metrics import Metrics
from codeagent.memory.governance_models import MemoryCandidate
from codeagent.runtime.agent_runtime import WorkerRun


@dataclass(frozen=True, slots=True)
class SharedMemoryReport:
    collected: int = 0  # Worker 产出的候选总数（去重前）
    staged: int = 0  # 实际新入库（去重、越界过滤后）
    rejected: int = 0  # 被 writable_types 拒绝的越界候选


@runtime_checkable
class SharedCandidateSink(Protocol):
    async def stage_shared_candidates(
        self, candidates: tuple[MemoryCandidate, ...]
    ) -> int: ...


class SupervisorMemoryWriter:
    def __init__(self, sink: SharedCandidateSink, *, metrics: Metrics | None = None) -> None:
        self._sink = sink
        self._metrics = metrics or Metrics()

    async def collect_and_stage(self, workers: dict[str, WorkerRun]) -> SharedMemoryReport:
        collected = 0
        rejected = 0
        deduped: dict[str, MemoryCandidate] = {}
        for worker in workers.values():
            writable = worker.run.definition.memory_profile
            for candidate in worker.result.memory_candidates:
                collected += 1
                if not writable.can_write(candidate.proposed_type):
                    rejected += 1
                    continue  # 越界候选：该 Agent 无权产此类型 Memory
                deduped[candidate.candidate_key] = candidate
        staged = 0
        if deduped:
            try:
                staged = await self._sink.stage_shared_candidates(tuple(deduped.values()))
            except Exception:
                # 保守失败：集中 staging 出错不打断编排。
                self._metrics.incr("memory.shared.stage_failures")
                staged = 0
        self._metrics.incr("memory.shared.collected", collected)
        self._metrics.incr("memory.shared.staged", staged)
        self._metrics.incr("memory.shared.rejected", rejected)
        return SharedMemoryReport(collected=collected, staged=staged, rejected=rejected)


class NullSupervisorMemoryWriter:
    """默认：不 staging（无 Memory 仓储 / stub 场景）。"""

    async def collect_and_stage(self, workers: dict[str, WorkerRun]) -> SharedMemoryReport:
        return SharedMemoryReport()


SupervisorWriter = SupervisorMemoryWriter | NullSupervisorMemoryWriter

__all__ = [
    "NullSupervisorMemoryWriter",
    "SharedCandidateSink",
    "SharedMemoryReport",
    "SupervisorMemoryWriter",
    "SupervisorWriter",
]
