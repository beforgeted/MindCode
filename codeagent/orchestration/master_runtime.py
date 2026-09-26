"""MasterRuntime：编排闭环 plan → schedule → verify →（replan）→ merge → cleanup。

合并与 cleanup 的所有权都在这里（V1 §7.1）：Worker 分支在**验收通过后**才合并回 base，
worktree 在合并之后才回收；失败的 Worker 默认保留 worktree 作 evidence。
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
from codeagent.orchestration.task_graph import TaskGraph
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeWorkspaceManager
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
            precompleted = {sid: o for sid, o in record.outcomes.items() if o.completed}
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

        async def _checkpoint(step_id: str, worker: WorkerRun) -> None:
            await self._run_store.record_step(master_run_id, _to_outcome(step_id, worker))

        while True:
            result = await self._scheduler.run(
                graph,
                session_id=session_id,
                cancellation=cancellation,
                trace_id=master_run_id,
                skip=frozenset(skip),
                on_step_complete=_checkpoint,
            )
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
            skip = set()  # 重规划后图已变，不再沿用旧 skip 集合

        assert result is not None and verdict is not None
        merged, conflicts = await self._merge(graph, result, accepted=verdict.accept)
        for step_id in _merged_step_ids(graph, result, merged):
            await self._run_store.mark_merged(
                master_run_id, step_id, result.workers[step_id].workspace.branch_name or ""
            )
        await self._cleanup(result)
        # Supervisor 集中 staging Worker 产出的 MemoryCandidate（Worker 从不直写）。
        await self._memory_writer.collect_and_stage(result.workers)

        # 聚合本次跑的 Worker 与恢复时跳过的已完成 Step。
        files: list[FileState] = []
        evidence: list[EvidenceRef] = []
        for worker in result.workers.values():
            files.extend(worker.result.files)
            evidence.extend(worker.result.evidence_refs)
        for outcome in precompleted.values():
            files.extend(outcome.files)
            evidence.extend(outcome.evidence_refs)

        await self._run_store.update_run_status(
            master_run_id, "success" if verdict.accept else "failed"
        )

        return FinalResult(
            task=task,
            accepted=verdict.accept,
            reason=verdict.reason,
            master_run_id=master_run_id,
            scheduler=result,
            files=tuple(files),
            evidence_refs=tuple(evidence),
            merged_branches=tuple(merged),
            merge_conflicts=tuple(conflicts),
            replans=replans,
        )

    async def _merge(
        self, graph: TaskGraph, result: SchedulerResult, *, accepted: bool
    ) -> tuple[list[str], list[str]]:
        merged: list[str] = []
        conflicts: list[str] = []
        if not accepted or not isinstance(self._wsm, GitWorktreeWorkspaceManager):
            return merged, conflicts
        # 按 graph 声明顺序合并已完成的 Worker 分支。
        for step in graph.steps:
            worker = result.workers.get(step.id)
            if worker is None or step.id not in result.completed:
                continue
            if not worker.workspace.is_isolated or not worker.workspace.branch_name:
                continue
            try:
                # 先把 worktree 里的改动提交到分支，否则 merge 带不回 base。
                committed = await self._wsm.commit(worker.workspace)
                if not committed:
                    continue
                await self._wsm.merge(worker.workspace)
                merged.append(worker.workspace.branch_name)
            except GitWorktreeError as exc:
                conflicts.append(f"{worker.workspace.branch_name}: {exc}")
                self._metrics.incr("master.merge_conflicts")
                break  # 冲突即停，交由人工处理，不自动解冲突。
        return merged, conflicts

    async def _cleanup(self, result: SchedulerResult) -> None:
        for step_id, worker in result.workers.items():
            keep = step_id in result.failed  # 失败的 worktree 保留作 evidence。
            try:
                await self._wsm.cleanup(worker.workspace, keep=keep)
            except Exception:
                self._metrics.incr("master.cleanup_failures")


def _to_outcome(step_id: str, worker: WorkerRun) -> StepOutcome:
    ok = worker.verification.ok and worker.result.ok
    return StepOutcome(
        step_id=step_id,
        status="completed" if ok else "failed",
        summary=worker.result.summary,
        files=worker.result.files,
        evidence_refs=worker.result.evidence_refs,
        branch_name=worker.workspace.branch_name,
        merged=False,
    )


def _merged_step_ids(
    graph: TaskGraph, result: SchedulerResult, merged_branches: list[str]
) -> list[str]:
    """把已合并的分支名映射回 step_id，供 RunStore 标记 merged。"""
    branches = set(merged_branches)
    out: list[str] = []
    for step in graph.steps:
        worker = result.workers.get(step.id)
        if worker is not None and worker.workspace.branch_name in branches:
            out.append(step.id)
    return out
