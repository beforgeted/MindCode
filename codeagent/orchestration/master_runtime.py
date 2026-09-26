"""MasterRuntime：编排闭环 plan → (schedule+integrate) → verify →（replan）→ cleanup。

集成不再是末尾的一次性大合并：它已被编进 StepScheduler 的执行环（验收即串行集成,
后继只有在前驱 INTEGRATED 后才派发,见 step_scheduler / integration_coordinator）。
MasterRuntime 只负责：规划、驱动调度、集成后的全局验收与重规划、以及 cleanup。

cleanup 所有权仍在这里：**已集成**的 worktree 才回收;未集成（失败/冲突）的保留作证据,
也便于 resume。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeagent.agent.models import FileState
from codeagent.evidence.models import EvidenceRef
from codeagent.infra.ids import new_id
from codeagent.infra.metrics import Metrics
from codeagent.orchestration.global_verifier import GlobalVerifier
from codeagent.orchestration.planner import Planner
from codeagent.orchestration.run_store import NullRunStore, RunStore, StepOutcome
from codeagent.orchestration.shared_memory import (
    NullSupervisorMemoryWriter,
    SupervisorWriter,
)
from codeagent.orchestration.step_scheduler import SchedulerResult, StepScheduler
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.workspace.manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class FinalResult:
    task: str
    accepted: bool
    reason: str = ""
    master_run_id: str = ""
    scheduler: SchedulerResult | None = None
    files: tuple[FileState, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    merged_branches: tuple[str, ...] = ()
    merge_conflicts: tuple[str, ...] = ()
    replans: int = 0
    # accepted 反映全局验收；integrated 还要求全部 Step 已干净并回 base（无失败/冲突）。
    integrated: bool = True


class MasterRuntime:
    def __init__(
        self,
        *,
        planner: Planner,
        scheduler: StepScheduler,
        global_verifier: GlobalVerifier,
        workspace_manager: WorkspaceManager,
        max_replans: int = 1,
        memory_writer: SupervisorWriter | None = None,
        run_store: RunStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._planner = planner
        self._scheduler = scheduler
        self._verifier = global_verifier
        self._wsm = workspace_manager
        self._max_replans = max(0, max_replans)
        self._memory_writer = memory_writer or NullSupervisorMemoryWriter()
        self._run_store = run_store or NullRunStore()
        self._metrics = metrics or Metrics()

    async def run(
        self,
        task: str,
        *,
        session_id: str,
        cancellation=None,
        resume_master_run_id: str | None = None,
    ) -> FinalResult:
        self._metrics.incr("master.runs")

        precompleted: dict[str, StepOutcome] = {}
        skip: set[str] = set()
        if resume_master_run_id is not None:
            record = await self._run_store.load_run(resume_master_run_id)
            if record is None:
                raise ValueError(f"找不到可恢复的 master run: {resume_master_run_id}")
            self._metrics.incr("master.resumes")
            master_run_id = record.master_run_id
            task = record.task
            graph = record.graph
            # 只跳过**已集成**的 Step；执行过但未集成的必须重跑（其分支可能已过期）。
            precompleted = {sid: o for sid, o in record.outcomes.items() if o.integrated}
            skip = set(precompleted)
            await self._run_store.update_run_status(master_run_id, "running")
        else:
            master_run_id = new_id("mrun")
            graph = await self._planner.plan(task)
            await self._run_store.save_run(
                master_run_id=master_run_id,
                session_id=session_id,
                task=task,
                graph=graph,
                status="running",
            )

        result: SchedulerResult | None = None
        verdict = None
        replans = 0
        current_task = task

        async def _checkpoint(step_id: str, worker: WorkerRun, integrated: bool) -> None:
            await self._run_store.record_step(
                master_run_id, _to_outcome(step_id, worker, integrated)
            )

        while True:
            # 调度内部已完成"验收即串行集成";返回时 integrated 集合即已并回 base 的 Step。
            result = await self._scheduler.run(
                graph,
                session_id=session_id,
                cancellation=cancellation,
                trace_id=master_run_id,
                skip=frozenset(skip),
                on_step_complete=_checkpoint,
            )
            # 全局验收发生在集成之后,针对真实集成产物（此处 result 的 failed 已含集成失败）。
            verdict = await self._verifier.verify(current_task, graph, result)
            if verdict.accept or replans >= self._max_replans:
                break
            replans += 1
            self._metrics.incr("master.replans")
            current_task = verdict.replan_instruction or task
            graph = await self._planner.plan(current_task)
            await self._run_store.save_run(
                master_run_id=master_run_id,
                session_id=session_id,
                task=current_task,
                graph=graph,
                status="running",
            )
            skip = set()  # 重规划后图已变,不再沿用旧 skip 集合

        assert result is not None and verdict is not None
        await self._cleanup(result)
        # Supervisor 集中 staging Worker 产出的 MemoryCandidate（Worker 从不直写）。
        await self._memory_writer.collect_and_stage(result.workers)

        files: list[FileState] = []
        evidence: list[EvidenceRef] = []
        for worker in result.workers.values():
            files.extend(worker.result.files)
            evidence.extend(worker.result.evidence_refs)
        for outcome in precompleted.values():
            files.extend(outcome.files)
            evidence.extend(outcome.evidence_refs)

        integrated_ok = verdict.accept and not result.failed and not result.conflicts
        await self._run_store.update_run_status(
            master_run_id, "success" if integrated_ok else "failed"
        )

        return FinalResult(
            task=task,
            accepted=verdict.accept,
            reason=verdict.reason,
            master_run_id=master_run_id,
            scheduler=result,
            files=tuple(files),
            evidence_refs=tuple(evidence),
            merged_branches=tuple(result.integrated_branches),
            merge_conflicts=tuple(result.conflicts),
            replans=replans,
            integrated=integrated_ok,
        )

    async def _cleanup(self, result: SchedulerResult) -> None:
        for step_id, worker in result.workers.items():
            # 已集成的才回收;未集成（失败/冲突）保留 worktree 与分支作证据、便于 resume。
            keep = step_id not in result.integrated
            try:
                await self._wsm.cleanup(worker.workspace, keep=keep)
            except Exception:
                self._metrics.incr("master.cleanup_failures")


def _to_outcome(step_id: str, worker: WorkerRun, integrated: bool) -> StepOutcome:
    return StepOutcome(
        step_id=step_id,
        status="integrated" if integrated else "failed",
        summary=worker.result.summary,
        files=worker.result.files,
        evidence_refs=worker.result.evidence_refs,
        branch_name=worker.workspace.branch_name,
        merged=integrated,
    )
