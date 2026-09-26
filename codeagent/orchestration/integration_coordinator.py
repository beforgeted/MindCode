"""IntegrationCoordinator：把已验收的 Worker 分支**串行、确定性**地集成回 base（Phase 1）。

为什么要有它：原来所有 Worker 从同一旧 base 并行起步、末尾一次性大合并 —— 导致 DAG
后继在派发时看不到前驱改动（后继 worktree 从旧 HEAD 切出）。改成"验收即集成"后,后继
只有在前驱 INTEGRATED 后才派发,其 worktree 自然从含前驱改动的最新 HEAD 切出。

职责边界:只负责"把一个 Worker 分支并入 base 并报告结果"。派发门控、顺序选择在
StepScheduler;冲突时的重跑收敛/Integrator 兜底留待 Phase 2/3。

- 隔离(git worktree):commit worker 分支 → merge 回 base;冲突已由 git_worktree.merge
  内部 `git merge --abort` 回滚,base 保持干净,这里如实报冲突。
- 非隔离(共享 root):改动已直接落在共享工作区(由 StepScheduler 写锁串行化),集成即 no-op。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeagent.infra.metrics import Metrics
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeWorkspaceManager
from codeagent.workspace.manager import WorkspaceManager


@dataclass(frozen=True, slots=True)
class IntegrationOutcome:
    integrated: bool
    branch: str | None = None
    conflict: str | None = None


class IntegrationCoordinator:
    def __init__(
        self, workspace_manager: WorkspaceManager | None = None, *, metrics: Metrics | None = None
    ) -> None:
        self._wsm = workspace_manager
        self._metrics = metrics or Metrics()

    async def integrate(self, worker: WorkerRun) -> IntegrationOutcome:
        ws = worker.workspace
        # 非隔离 / 无分支 / 无 git 管理器:改动已在共享 base,集成是 no-op。
        if (
            not ws.is_isolated
            or not ws.branch_name
            or not isinstance(self._wsm, GitWorktreeWorkspaceManager)
        ):
            return IntegrationOutcome(integrated=True, branch=ws.branch_name)
        try:
            committed = await self._wsm.commit(ws)
            if committed:
                await self._wsm.merge(ws)  # 冲突时内部已 merge --abort,base 保持干净
                self._metrics.incr("integration.merged")
            return IntegrationOutcome(integrated=True, branch=ws.branch_name)
        except GitWorktreeError as exc:
            self._metrics.incr("integration.conflicts")
            return IntegrationOutcome(integrated=False, branch=ws.branch_name, conflict=str(exc))


__all__ = ["IntegrationCoordinator", "IntegrationOutcome"]
