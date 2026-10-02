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

import asyncio
from dataclasses import dataclass, replace

from codeagent.agent.models import FileState
from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.evidence.models import EvidenceRef
from codeagent.execution.models import ExecutionPurpose
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.publication import PublicationUncertain
from codeagent.execution.workspace import capture_workspace
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.infra.ids import new_id
from codeagent.infra.metrics import Metrics
from codeagent.infra.trace import trace_scope, update_trace
from codeagent.observability import TrajectoryExporter
from codeagent.orchestration.global_verifier import GlobalVerifier, VerificationTarget
from codeagent.orchestration.planner import Planner
from codeagent.orchestration.run_store import (
    AttemptRecord,
    AttemptState,
    NullRunStore,
    RunRecord,
    RunStore,
    StepOutcome,
)
from codeagent.orchestration.shared_memory import NullSupervisorMemoryWriter, SupervisorWriter
from codeagent.orchestration.step_scheduler import SchedulerResult, StepScheduler
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.tool.approval import ApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.command_policy import CommandDecision
from codeagent.tool.deferred import DeferredAction, DeferredRecord, DeferredState
from codeagent.tool.effects import RetryPolicy
from codeagent.tool.executor import CommandExecutor, LocalExecutor
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
from codeagent.workspace.manager import WorkspaceManager
from codeagent.workspace.snapshot import SnapshotWorkspaceManager


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
    # post-promote 外部动作执行统计（A2）。
    deferred_executed: int = 0
    deferred_failed: int = 0
    deferred_skipped: int = 0
    deferred_unknown: int = 0
    deferred_records: tuple[DeferredRecord, ...] = ()
    trajectory_path: str | None = None
    trajectory_error: str | None = None


class MasterRuntime:
    def __init__(
        self,
        *,
        planner: Planner,
        scheduler: StepScheduler,
        global_verifier: GlobalVerifier,
        workspace_manager: WorkspaceManager,
        max_replans: int = 1,
        promote_max_retries: int = 2,
        verify_command: str | None = None,
        memory_writer: SupervisorWriter | None = None,
        run_store: RunStore | None = None,
        metrics: Metrics | None = None,
        approval_policy: ApprovalPolicy | None = None,
        command_executor: CommandExecutor | None = None,
        artifact_store: ArtifactStore | None = None,
        trajectory_exporter: TrajectoryExporter | None = None,
        trajectory_timeout_seconds: float = 10.0,
        sandbox_manager: PodmanSandboxManager | None = None,
    ) -> None:
        self._planner = planner
        self._scheduler = scheduler
        self._verifier = global_verifier
        self._wsm = workspace_manager
        self._max_replans = max(0, max_replans)
        self._promote_max_retries = max(0, promote_max_retries)
        self._verify_command = verify_command
        self._memory_writer = memory_writer or NullSupervisorMemoryWriter()
        self._run_store = run_store or NullRunStore()
        self._metrics = metrics or Metrics()
        # A2：post-promote 执行被延后的外部动作。默认 fail-safe 拒绝；
        # 无 artifact_store 则只上报不执行。
        self._approval = approval_policy or DenyExternalApprovalPolicy()
        self._executor = command_executor or LocalExecutor()
        self._artifacts = artifact_store
        self._trajectory_exporter = trajectory_exporter
        self._trajectory_timeout = trajectory_timeout_seconds
        self._sandbox = sandbox_manager

    async def run(
        self, task: str, *, session_id: str, cancellation=None,
        resume_master_run_id: str | None = None,
    ) -> FinalResult:
        master_run_id = resume_master_run_id or new_id("mrun")
        if isinstance(self._wsm, SnapshotWorkspaceManager) and resume_master_run_id is not None:
            return FinalResult(
                task=task, accepted=False, integrated=False, master_run_id=master_run_id,
                reason="非Git沙箱任务暂不支持resume；请核对实际成果后启动新任务",
            )
        path = error = None
        owns_snapshot = False
        with trace_scope(session_id=session_id, master_run_id=master_run_id,
                         invocation_id=new_id("inv")):
            try:
                snapshot = self._wsm if isinstance(self._wsm, SnapshotWorkspaceManager) else None
                if snapshot is not None:
                    snapshot.begin()
                    owns_snapshot = True
                final = await self._run(
                    task, session_id=session_id, cancellation=cancellation,
                    resume_master_run_id=resume_master_run_id, master_run_id=master_run_id,
                )
            except asyncio.CancelledError:
                if isinstance(self._wsm, SnapshotWorkspaceManager) and owns_snapshot:
                    await self._run_store.update_run_status(master_run_id, "cancelled")
                raise
            except Exception as exc:
                if not isinstance(self._wsm, SnapshotWorkspaceManager):
                    raise
                uncertain = isinstance(exc, PublicationUncertain) or self._wsm.published
                await self._run_store.update_run_status(
                    master_run_id, "unknown" if uncertain else
                    "cancelled" if isinstance(exc, CancelledByUser) else "failed",
                )
                final = FinalResult(
                    task=task, accepted=False, integrated=False, master_run_id=master_run_id,
                    reason=("写回结果待核对；不能认为全部成功或全部回滚。" if uncertain else "")
                    + f"{type(exc).__name__}: {exc}",
                )
            finally:
                if owns_snapshot and isinstance(self._wsm, SnapshotWorkspaceManager):
                    self._wsm.end()
                if self._trajectory_exporter is not None:
                    try:
                        async with asyncio.timeout(self._trajectory_timeout):
                            path = str(await self._trajectory_exporter.export(
                                master_run_id, session_snapshot=self._metrics.snapshot(),
                            ))
                    except Exception as exc:
                        self._metrics.incr("observability.export_failures")
                        error = type(exc).__name__
        return replace(final, trajectory_path=path, trajectory_error=error)

    async def aclose(self) -> None:
        if self._sandbox is not None:
            await self._sandbox.aclose()

    @property
    def supports_resume(self) -> bool:
        return not isinstance(self._wsm, SnapshotWorkspaceManager)

    async def _run(
        self,
        task: str,
        *,
        session_id: str,
        cancellation=None,
        resume_master_run_id: str | None = None,
        master_run_id: str,
    ) -> FinalResult:
        self._metrics.incr("master.runs")
        git = self._wsm if isinstance(
            self._wsm, (GitWorktreeWorkspaceManager, SnapshotWorkspaceManager),
        ) else None

        attempt_offset = 0
        if resume_master_run_id is not None:
            record = await self._run_store.load_run(resume_master_run_id)
            if record is None:
                raise ValueError(f"找不到可恢复的 master run: {resume_master_run_id}")
            self._metrics.incr("master.resumes")
            recovery_git = git if isinstance(git, GitWorktreeWorkspaceManager) else None
            recovered = await self._try_recover(record, recovery_git)
            if recovered is not None:
                # Git 成果已完成，仍须恢复同一 Attempt 的外部动作。
                attempt_no = record.last_attempt.attempt_no if record.last_attempt else 1
                pending = await self._run_store.load_deferred(record.master_run_id, attempt_no)
                records = await self._execute_deferred(
                    pending, cancellation, master_run_id=record.master_run_id,
                    attempt_no=attempt_no,
                )
                return _with_deferred(recovered, records)
            master_run_id, task, graph = record.master_run_id, record.task, record.graph
            await self._reclaim_orphans(record, recovery_git)
            # 关键：用**持久化的** original_base_sha，不用当前 HEAD（可能已被某次 promote 移动）。
            original_base = record.original_base_sha or (await git.base_revision() if git else None)
            attempt_offset = record.last_attempt.attempt_no if record.last_attempt else 0
        else:
            graph = await self._planner.plan(task)
            original_base = await git.base_revision() if git else None
        await self._run_store.save_run(
            master_run_id=master_run_id, session_id=session_id, task=task,
            graph=graph, status="running", original_base_sha=original_base,
        )

        async def _checkpoint(step_id: str, worker: WorkerRun, integrated: bool) -> None:
            await self._run_store.record_step(
                master_run_id, replace(
                    _to_outcome(step_id, worker, integrated), attempt_no=attempt_no,
                    agent_run_id=worker.run.run_id,
                )
            )

        current_task, attempts = task, 0
        replans_left = self._max_replans
        promote_retries_left = self._promote_max_retries
        result: SchedulerResult | None = None
        verdict = None
        integrated_ok = False
        promoted_sha: str | None = None
        reason = ""

        while True:
            if isinstance(git, SnapshotWorkspaceManager) and cancellation is not None:
                cancellation.raise_if_cancelled()
            attempts += 1
            attempt_no = attempt_offset + attempts  # 恢复时接着已有 attempt_no，避免 PK 冲突
            update_trace(attempt_no=attempt_no)
            candidate = (
                await git.create_candidate(original_base)
                if git and original_base is not None
                else None
            )
            if candidate is not None:
                await self._save_attempt(
                    master_run_id, attempt_no, AttemptState.RUNNING,
                    original_base=original_base, candidate_branch=candidate.branch_name,
                )
            result = await self._scheduler.run(
                graph, session_id=session_id, cancellation=cancellation,
                trace_id=master_run_id, on_step_complete=_checkpoint, candidate=candidate,
            )
            candidate_sha = await git.head(candidate.root) if (git and candidate) else None
            if candidate is not None:
                await self._update_attempt(
                    master_run_id, attempt_no, AttemptState.CANDIDATE_FROZEN,
                    candidate_sha=candidate_sha,
                )
            target = await self._build_target(
                git, original_base, candidate_sha, cancellation=cancellation,
            )
            if candidate is not None:
                await self._update_attempt(master_run_id, attempt_no, AttemptState.VERIFYING)
            verdict = await self._verifier.verify(current_task, graph, result, target)

            steps_ok = not result.failed and not result.blocked
            deterministic_ok = target is None or target.deterministic_ok is not False
            accept = verdict.accept and not verdict.indeterminate and steps_ok and deterministic_ok

            if accept:
                if candidate is not None:
                    await self._update_attempt(
                        master_run_id, attempt_no, AttemptState.VERIFIED, verdict=verdict.reason,
                    )
                # 清单必须先于 promote 持久化；失败直接中断，不能吞异常后继续合并。
                await self._run_store.save_deferred(
                    master_run_id, attempt_no,
                    [a for w in result.workers.values() for a in w.run.deferred_actions],
                )

            if accept and git and candidate_sha and original_base is not None:
                # PROMOTING 必须在 git.promote 之前落库（带 candidate_sha）→ 崩溃恢复可幂等判定。
                await self._update_attempt(
                    master_run_id, attempt_no, AttemptState.PROMOTING, candidate_sha=candidate_sha
                )
                if isinstance(git, SnapshotWorkspaceManager) and cancellation is not None:
                    cancellation.raise_if_cancelled()
                promoted = await git.promote(candidate_sha, expected_base=original_base)
                await self._discard_attempt(candidate, result)
                if promoted:
                    await self._update_attempt(master_run_id, attempt_no, AttemptState.PROMOTED)
                    integrated_ok, promoted_sha, reason = True, candidate_sha, verdict.reason
                    break
                await self._update_attempt(
                    master_run_id, attempt_no, AttemptState.DISCARDED, verdict="base_stale"
                )
                # BASE_STALE 走**独立**的 promote 重试预算，不吃语义 replan 预算。
                if promote_retries_left <= 0:
                    reason = "真实 base 被外部反复推进(BASE_STALE)，超出 promote 重试预算"
                    break
                promote_retries_left -= 1
                self._metrics.incr("master.promote_retries")
                reason = "真实 base 被外部推进(BASE_STALE)，刷新后重试"
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
                    master_run_id, attempt_no, AttemptState.DISCARDED,
                    verdict=reason[:200] or "reject",
                )
            await self._discard_attempt(candidate, result)
            if replans_left <= 0:
                break
            replans_left -= 1
            self._metrics.incr("master.replans")
            current_task = verdict.replan_instruction or task
            graph = await self._planner.plan(current_task)

        assert result is not None and verdict is not None
        # 只 stage 成功 promote（或非 git accept）那次 Attempt 的 Worker 候选；
        # 被丢弃的 Attempt（reject / BASE_STALE / 失败）不 stage（不变式 1、5，C7）。
        if integrated_ok:
            await self._memory_writer.collect_and_stage(result.workers)

        files: list[FileState] = []
        evidence: list[EvidenceRef] = []
        deferred: list[DeferredAction] = []
        for worker in result.workers.values():
            files.extend(worker.result.files)
            evidence.extend(worker.result.evidence_refs)
            deferred.extend(worker.run.deferred_actions)
        if isinstance(git, SnapshotWorkspaceManager):
            files = list(git.published_files()) if integrated_ok else []

        await self._run_store.update_run_status(
            master_run_id, "success" if integrated_ok else "failed", promoted_sha=promoted_sha
        )
        # A2：promote 成功后才执行被延后的外部动作（在真实 base workspace）；否则不执行。
        records: tuple[DeferredRecord, ...] = ()
        if integrated_ok and deferred:
            records = await self._execute_deferred(
                tuple(DeferredRecord(a) for a in deferred), cancellation,
                master_run_id=master_run_id, attempt_no=attempt_no,
            )
        final = FinalResult(
            task=task,
            accepted=integrated_ok,
            reason=reason,
            master_run_id=master_run_id,
            scheduler=result,
            files=tuple(files) if not isinstance(git, SnapshotWorkspaceManager)
            or integrated_ok else (),
            evidence_refs=tuple(evidence),
            merged_branches=tuple(result.integrated_branches),
            merge_conflicts=tuple(result.conflicts),
            replans=max(0, attempts - 1),
            integrated=integrated_ok,
            deferred_actions=tuple(deferred),
        )
        return _with_deferred(final, records) if integrated_ok else final

    async def _execute_deferred(
        self, deferred: tuple[DeferredRecord, ...], cancellation: CancellationToken | None,
        *, master_run_id: str, attempt_no: int,
    ) -> tuple[DeferredRecord, ...]:
        """post-promote 逐条处理延后的外部动作：审批 → 在真实 base 执行。

        无 artifact_store（编程/测试装配未提供）时一律只上报不执行（skipped）。
        NEVER 只尝试一次；IDEMPOTENT 允许有限重试。执行失败不回滚已 promote 的 Git 成果。
        """
        git = self._wsm if isinstance(self._wsm, GitWorktreeWorkspaceManager) else None
        repo_root = git.repo_root if git else None
        records: list[DeferredRecord] = []
        seen: set[str] = set()
        for record in deferred:
            action = record.action
            if action.id in seen:  # 同一条动作只处理一次（幂等键）
                continue
            seen.add(action.id)
            limit = 2 if action.retry is RetryPolicy.IDEMPOTENT else 1
            if record.state == DeferredState.RUNNING and (
                action.retry is not RetryPolicy.IDEMPOTENT or record.attempts >= limit
            ):
                record = replace(record, state=DeferredState.UNKNOWN)
                await self._run_store.update_deferred(master_run_id, attempt_no, record)
            if record.state not in (DeferredState.PENDING, DeferredState.RUNNING):
                records.append(record)
                continue
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            approved = self._sandbox is None and await self._approval.approve(
                _decision_of(action), command=action.command,
            )
            if not approved or self._artifacts is None or repo_root is None:
                # 已开始的动作拒绝重试时，原执行结果仍然未知。
                state = (DeferredState.UNKNOWN if record.state == DeferredState.RUNNING
                         else DeferredState.SKIPPED)
                record = replace(record, state=state)
                await self._run_store.update_deferred(master_run_id, attempt_no, record)
                records.append(record)
                self._metrics.incr("master.deferred_skipped")
                continue
            ok = False
            while record.attempts < limit and not ok:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                record = replace(record, state=DeferredState.RUNNING, attempts=record.attempts + 1)
                # 不吞持久化错误：必须先记账，才允许产生外部副作用。
                await self._run_store.update_deferred(master_run_id, attempt_no, record)
                uncertain = False
                try:
                    outcome = await self._executor.run(
                        command=action.command,
                        cwd=repo_root,
                        cancellation=cancellation or CancellationToken(),
                        artifact_store=self._artifacts,
                        max_output_bytes=1 << 20,
                        metadata={"deferred": action.id},
                    )
                    ok = outcome.exit_code == 0
                except CancelledByUser:
                    raise
                except Exception:
                    ok = False
                    uncertain = True
                state = (DeferredState.SUCCEEDED if ok else
                         DeferredState.UNKNOWN if uncertain else DeferredState.FAILED)
                if not ok and record.attempts < limit:
                    state = DeferredState.RUNNING if uncertain else DeferredState.PENDING
                # 结果落库失败则保留 RUNNING，恢复按结果未知处理。
                record = replace(record, state=state)
                await self._run_store.update_deferred(master_run_id, attempt_no, record)
            if ok:
                self._metrics.incr("master.deferred_executed")
            else:
                self._metrics.incr("master.deferred_failed")
            records.append(record)
        return tuple(records)

    async def _try_recover(
        self, record: RunRecord, git: GitWorktreeWorkspaceManager | None
    ) -> FinalResult | None:
        """幂等恢复：若 run 已完成 / promote 已发生（或可安全补做），直接返回结果；
        否则返回 None 交由调用方回收孤儿后从持久 original_base 重开。"""
        if record.status == "success":
            return self._recovered_result(record, reason="run 已完成（恢复无操作）")
        last = record.last_attempt
        if last is None or git is None:
            return None
        if last.state == AttemptState.PROMOTED:
            await self._run_store.update_run_status(
                record.master_run_id, "success", promoted_sha=last.candidate_sha
            )
            return self._recovered_result(record, reason="已 promote（恢复补记 success）")
        if last.state == AttemptState.PROMOTING and last.candidate_sha:
            head = await git.head()
            if head == last.candidate_sha:
                # promote 其实已成功、只是状态没落库 → 识别为已完成，绝不重复推进。
                await self._finish_recovered_promote(record, last.attempt_no, last.candidate_sha)
                return self._recovered_result(record, reason="promote 已成功（恢复识别，未重推）")
            if last.original_base_sha and head == last.original_base_sha:
                try:
                    promoted = await git.promote(
                        last.candidate_sha, expected_base=last.original_base_sha
                    )
                except Exception:
                    promoted = False
                if promoted:
                    await self._finish_recovered_promote(
                        record, last.attempt_no, last.candidate_sha
                    )
                    return self._recovered_result(record, reason="promote 恢复重试成功")
            # HEAD 既非 candidate 也非 original_base（BASE_STALE），或重试失败 → 丢弃重开。
            await self._update_attempt(
                record.master_run_id, last.attempt_no, AttemptState.DISCARDED,
                verdict="base_stale(recover)",
            )
        return None

    async def _finish_recovered_promote(
        self, record: RunRecord, attempt_no: int, candidate_sha: str
    ) -> None:
        await self._update_attempt(record.master_run_id, attempt_no, AttemptState.PROMOTED)
        await self._run_store.update_run_status(
            record.master_run_id, "success", promoted_sha=candidate_sha
        )

    def _recovered_result(self, record: RunRecord, *, reason: str) -> FinalResult:
        return FinalResult(
            task=record.task,
            accepted=True,
            reason=reason,
            master_run_id=record.master_run_id,
            scheduler=None,
            integrated=True,
        )

    async def _reclaim_orphans(
        self, record: RunRecord, git: GitWorktreeWorkspaceManager | None
    ) -> None:
        """回收崩溃遗留的孤儿 worktree/branch。resume 将开全新 attempt，旧分支全是孤儿。"""
        if git is None:
            return
        try:
            removed = await git.reclaim_orphans(keep_branches=set())
            if removed:
                self._metrics.incr("master.orphans_reclaimed", removed)
        except Exception:
            self._metrics.incr("master.cleanup_failures")

    async def _build_target(
        self,
        git: GitWorktreeWorkspaceManager | SnapshotWorkspaceManager | None,
        original_base: str | None,
        candidate_sha: str | None,
        *, cancellation: CancellationToken | None = None,
    ) -> VerificationTarget | None:
        if git is None or original_base is None or candidate_sha is None:
            return None
        changed = await git.changed_files(original_base, candidate_sha)
        diff = await git.diff_text(original_base, candidate_sha)
        det_ok: bool | None = None
        det_detail = ""
        if isinstance(git, SnapshotWorkspaceManager) and changed and not self._verify_command:
            det_ok = False
            det_detail = "非Git任务包含改动但未配置 CODEAGENT_VERIFY_CMD"
        if self._verify_command:
            val = await git.create_validation(candidate_sha)
            try:
                if self._sandbox is None:
                    if not isinstance(git, GitWorktreeWorkspaceManager):
                        raise RuntimeError("非Git快照任务必须使用沙箱验收")
                    code, out = await git.run_check(val.root, self._verify_command)
                else:
                    handle = await self._sandbox.open(
                        capture_workspace(val, self._sandbox.snapshot_limits),
                        ExecutionPurpose.VALIDATION,
                    )
                    try:
                        output = await self._sandbox.execute(
                            handle, self._verify_command, cancellation=cancellation,
                        )
                        code = output.returncode
                        out = (output.stdout + output.stderr).decode("utf-8", errors="replace")
                    finally:
                        # Validation changes are never published to the candidate.
                        await asyncio.shield(self._sandbox.close(handle))
                det_ok = code == 0
                if not det_ok:
                    det_detail = out[-2000:]
            except Exception as exc:
                if self._sandbox is None:
                    raise
                det_ok = False
                det_detail = f"沙箱验收失败: {type(exc).__name__}: {exc}"
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
            raise

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
            raise

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


def _with_deferred(final: FinalResult, records: tuple[DeferredRecord, ...]) -> FinalResult:
    return replace(
        final,
        deferred_actions=tuple(r.action for r in records),
        deferred_records=records,
        deferred_executed=sum(r.state == DeferredState.SUCCEEDED for r in records),
        deferred_failed=sum(r.state == DeferredState.FAILED for r in records),
        deferred_skipped=sum(r.state == DeferredState.SKIPPED for r in records),
        deferred_unknown=sum(r.state == DeferredState.UNKNOWN for r in records),
    )


def _decision_of(action: DeferredAction) -> CommandDecision:
    return CommandDecision(
        allowed=True, effect=action.effect, retry=action.retry,
        needs_approval=True, reason=action.reason,
    )


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
