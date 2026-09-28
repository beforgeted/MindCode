"""MasterRuntime：Master Attempt Transaction —— 每次尝试一个 candidate，产物级验收，
CAS 原子推进真实 base，否则丢弃整个 Attempt 从 original_base 重开。

为什么：并行子 Agent 的集成必须是事务。若直接改真实 base、reject 后在已改 base 上重跑,
非幂等副作用会重复叠加（知识文档 004）。所以：
- 固定 original_base（run 开始时真实 base HEAD）。
- 每个 Attempt：建 candidate（从 original_base）→ 调度（Worker 从 candidate 切、集成进 candidate）
  → 冻结 candidate_sha → 产物级验收 → accept 则 CAS `merge --ff-only` 推进真实 base；
  reject/indeterminate 丢弃整个 Attempt、下一 Attempt 从 original_base 重开。
- 提交门禁 fail-closed：验证器不可用/不可解析 = indeterminate，绝不推进真实 base。

边界：candidate 只隔离**仓库内文件**;外部副作用（API/DB/树外写/发布）不被隔离,见文档 004。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeagent.agent.models import FileState
from codeagent.evidence.models import EvidenceRef
from codeagent.infra.ids import new_id
from codeagent.infra.metrics import Metrics
from codeagent.orchestration.global_verifier import GlobalVerifier, VerificationTarget
from codeagent.orchestration.planner import Planner
from codeagent.orchestration.run_store import (
    AttemptRecord,
    AttemptState,
    NullRunStore,
    RunStore,
    StepOutcome,
)
from codeagent.orchestration.shared_memory import NullSupervisorMemoryWriter, SupervisorWriter
from codeagent.orchestration.step_scheduler import SchedulerResult, StepScheduler
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.tool.deferred import DeferredAction
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
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
    # integrated ⟺ 成功 CAS 推进真实 base（产物级验收通过）。
    integrated: bool = True
    # 推测期被拦下的外部副作用（跨 Worker 汇总）。promote 后按 ApprovalPolicy 处理；
    # 最小实现：非交互默认只上报、不执行。
    deferred_actions: tuple[DeferredAction, ...] = ()


class MasterRuntime:
    def __init__(
        self,
        *,
        planner: Planner,
        scheduler: StepScheduler,
        global_verifier: GlobalVerifier,
        workspace_manager: WorkspaceManager,
        max_replans: int = 1,
        verify_command: str | None = None,
        memory_writer: SupervisorWriter | None = None,
        run_store: RunStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._planner = planner
        self._scheduler = scheduler
        self._verifier = global_verifier
        self._wsm = workspace_manager
        self._max_replans = max(0, max_replans)
        self._verify_command = verify_command
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
        git = self._wsm if isinstance(self._wsm, GitWorktreeWorkspaceManager) else None

        if resume_master_run_id is not None:
            record = await self._run_store.load_run(resume_master_run_id)
            if record is None:
                raise ValueError(f"找不到可恢复的 master run: {resume_master_run_id}")
            self._metrics.incr("master.resumes")
            master_run_id, task, graph = record.master_run_id, record.task, record.graph
        else:
            master_run_id = new_id("mrun")
            graph = await self._planner.plan(task)
        original_base = await git.base_revision() if git else None
        await self._run_store.save_run(
            master_run_id=master_run_id, session_id=session_id, task=task,
            graph=graph, status="running", original_base_sha=original_base,
        )

        async def _checkpoint(step_id: str, worker: WorkerRun, integrated: bool) -> None:
            await self._run_store.record_step(
                master_run_id, _to_outcome(step_id, worker, integrated)
            )

        max_attempts = self._max_replans + 1
        current_task, attempts = task, 0
        result: SchedulerResult | None = None
        verdict = None
        integrated_ok = False
        promoted_sha: str | None = None
        reason = ""

        while attempts < max_attempts:
            attempts += 1
            candidate = (
                await git.create_candidate(original_base)
                if git and original_base is not None
                else None
            )
            if candidate is not None:
                await self._save_attempt(
                    master_run_id, attempts, AttemptState.RUNNING,
                    original_base=original_base, candidate_branch=candidate.branch_name,
                )
            result = await self._scheduler.run(
                graph, session_id=session_id, cancellation=cancellation,
                trace_id=master_run_id, on_step_complete=_checkpoint, candidate=candidate,
            )
            candidate_sha = await git.head(candidate.root) if (git and candidate) else None
            if candidate is not None:
                await self._update_attempt(
                    master_run_id, attempts, AttemptState.CANDIDATE_FROZEN,
                    candidate_sha=candidate_sha,
                )
            target = await self._build_target(git, original_base, candidate_sha)
            if candidate is not None:
                await self._update_attempt(master_run_id, attempts, AttemptState.VERIFYING)
            verdict = await self._verifier.verify(current_task, graph, result, target)

            steps_ok = not result.failed and not result.blocked
            accept = verdict.accept and not verdict.indeterminate and steps_ok

            if accept and git and candidate_sha and original_base is not None:
                # PROMOTING 必须在 git.promote 之前落库（带 candidate_sha）→ 崩溃恢复可幂等判定。
                await self._update_attempt(
                    master_run_id, attempts, AttemptState.PROMOTING, candidate_sha=candidate_sha
                )
                promoted = await git.promote(candidate_sha, expected_base=original_base)
                await self._discard_attempt(candidate, result)
                if promoted:
                    await self._update_attempt(master_run_id, attempts, AttemptState.PROMOTED)
                    integrated_ok, promoted_sha, reason = True, candidate_sha, verdict.reason
                    break
                await self._update_attempt(
                    master_run_id, attempts, AttemptState.DISCARDED, verdict="base_stale"
                )
                reason = "真实 base 被外部推进(BASE_STALE)，已放弃本次推进"
                original_base = await git.base_revision()  # 刷新后重试
                continue
            if accept and not git:
                integrated_ok, reason = True, verdict.reason
                break

            # reject / indeterminate / 有失败 Step → 丢弃整个 Attempt,从 original_base 重开
            reason = verdict.reason or (
                "验证器不可用" if verdict.indeterminate else "存在未完成 Step"
            )
            if candidate is not None:
                await self._update_attempt(
                    master_run_id, attempts, AttemptState.DISCARDED, verdict=reason[:200] or "reject"
                )
            await self._discard_attempt(candidate, result)
            if attempts >= max_attempts:
                break
            self._metrics.incr("master.replans")
            current_task = verdict.replan_instruction or task
            graph = await self._planner.plan(current_task)

        assert result is not None and verdict is not None
        await self._memory_writer.collect_and_stage(result.workers)

        files: list[FileState] = []
        evidence: list[EvidenceRef] = []
        deferred: list[DeferredAction] = []
        for worker in result.workers.values():
            files.extend(worker.result.files)
            evidence.extend(worker.result.evidence_refs)
            deferred.extend(worker.run.deferred_actions)

        await self._run_store.update_run_status(
            master_run_id, "success" if integrated_ok else "failed", promoted_sha=promoted_sha
        )
        return FinalResult(
            task=task,
            accepted=integrated_ok,
            reason=reason,
            master_run_id=master_run_id,
            scheduler=result,
            files=tuple(files),
            evidence_refs=tuple(evidence),
            merged_branches=tuple(result.integrated_branches),
            merge_conflicts=tuple(result.conflicts),
            replans=max(0, attempts - 1),
            integrated=integrated_ok,
            deferred_actions=tuple(deferred),
        )

    async def _build_target(
        self,
        git: GitWorktreeWorkspaceManager | None,
        original_base: str | None,
        candidate_sha: str | None,
    ) -> VerificationTarget | None:
        if git is None or original_base is None or candidate_sha is None:
            return None
        changed = await git.changed_files(original_base, candidate_sha)
        diff = await git.diff_text(original_base, candidate_sha)
        det_ok: bool | None = None
        det_detail = ""
        if self._verify_command:
            val = await git.create_validation(candidate_sha)
            try:
                code, out = await git.run_check(val.root, self._verify_command)
                det_ok = code == 0
                if not det_ok:
                    det_detail = out[-2000:]
            finally:
                await git.cleanup(val, keep=False)
        return VerificationTarget(
            revision=candidate_sha,
            changed_files=tuple(sorted(changed)),
            diff=diff,
            deterministic_ok=det_ok,
            deterministic_detail=det_detail,
        )

    async def _save_attempt(
        self,
        master_run_id: str,
        attempt_no: int,
        state: str,
        *,
        original_base: str | None,
        candidate_branch: str | None,
    ) -> None:
        try:
            await self._run_store.save_attempt(
                master_run_id,
                AttemptRecord(
                    attempt_no=attempt_no, state=state,
                    original_base_sha=original_base, candidate_branch=candidate_branch,
                ),
            )
        except Exception:
            self._metrics.incr("master.checkpoint_failures")

    async def _update_attempt(
        self,
        master_run_id: str,
        attempt_no: int,
        state: str,
        *,
        candidate_sha: str | None = None,
        verdict: str | None = None,
    ) -> None:
        try:
            await self._run_store.update_attempt(
                master_run_id, attempt_no, state=state,
                candidate_sha=candidate_sha, verdict=verdict,
            )
        except Exception:
            self._metrics.incr("master.checkpoint_failures")

    async def _discard_attempt(
        self, candidate: WorkspaceContext | None, result: SchedulerResult
    ) -> None:
        """一次性回收本 Attempt 的所有 worktree/branch（Worker + candidate）。"""
        for worker in result.workers.values():
            try:
                await self._wsm.cleanup(worker.workspace, keep=False)
            except Exception:
                self._metrics.incr("master.cleanup_failures")
        if candidate is not None:
            try:
                await self._wsm.cleanup(candidate, keep=False)
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
