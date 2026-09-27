"""IntegrationCoordinator：把已验收的 Worker 分支串行、确定性地集成回 base，
并检测"推测执行是否过期"（Phase 1 + Phase 2）。

Phase 1：验收即集成,后继只有在前驱 INTEGRATED 后才派发（消除依赖型后继的过期）。
Phase 2：并行兄弟可能都从旧 base 起步 —— A 先集成后 B 仍基于旧版本。集成 B 时:
- 集成 HEAD 未动过（== B 的 base_revision）→ 直接合并（clean）。
- HEAD 已动、且期间改动文件与 B 的**读集∪写集**无重叠、且 B 没用过 run_command（读集已知）
  → 合并(改动不相交,通常 clean)。
- 有重叠 / 读集未知 / 合并冲突 → 判 **stale**,交回 StepScheduler 在最新 HEAD 上重跑。

关键:读写重叠不仅看 write/write（git 能发现),还看 read/write —— B 读过的文件被 A 改了,
即使 git 不冲突,B 的结果也可能基于过期行为,必须重跑。这是比 git 冲突更隐蔽的一类过期。

合并冲突由 git_worktree.merge 内部 `git merge --abort` 回滚,base 始终保持干净一致。
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from codeagent.infra.metrics import Metrics
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.workspace.git_worktree import GitWorktreeError, GitWorktreeWorkspaceManager
from codeagent.workspace.manager import WorkspaceManager

# 会"读"工作区、但读集可枚举的工具;run_command 读集不可知 → 保守判可能重叠。
_READ_TOOLS = {"read_file", "grep", "read_artifact"}
_UNKNOWN_READ_TOOLS = {"run_command"}


@dataclass(frozen=True, slots=True)
class IntegrationOutcome:
    status: str  # "integrated" | "stale" | "failed"
    branch: str | None = None
    conflict: str | None = None
    overlap: tuple[str, ...] = ()  # 与已集成改动重叠的文件（供 Integrator 交代冲突现场）

    @property
    def integrated(self) -> bool:
        return self.status == "integrated"

    @property
    def stale(self) -> bool:
        return self.status == "stale"


class IntegrationCoordinator:
    def __init__(
        self, workspace_manager: WorkspaceManager | None = None, *, metrics: Metrics | None = None
    ) -> None:
        self._wsm = workspace_manager
        self._metrics = metrics or Metrics()

    async def integrate(
        self, worker: WorkerRun, candidate: object | None = None
    ) -> IntegrationOutcome:
        from codeagent.workspace.context import WorkspaceContext

        ws = worker.workspace
        wsm = self._wsm
        if (
            not ws.is_isolated
            or not ws.branch_name
            or not isinstance(wsm, GitWorktreeWorkspaceManager)
            or not isinstance(candidate, WorkspaceContext)
        ):
            # 非隔离 / 无分支 / 无 git / 无 candidate：改动已在共享 base,集成是 no-op。
            return IntegrationOutcome("integrated", branch=ws.branch_name)

        committed = await wsm.commit(ws)
        if not committed:
            return IntegrationOutcome("integrated", branch=ws.branch_name)  # 无改动

        # Phase 2：candidate HEAD 是否在本 Worker 启动后动过?动过则查过期。
        if ws.base_revision:
            head = await wsm.head(candidate.root)
            if head != ws.base_revision:
                intervening = await wsm.changed_files(ws.base_revision, head)
                if intervening:
                    write_set = await wsm.branch_files(ws.base_revision, ws.branch_name)
                    read_set, reads_unknown = _read_info(worker)
                    overlap = intervening & (write_set | read_set)
                    if overlap or reads_unknown:
                        self._metrics.incr("integration.stale")
                        hint = tuple(sorted(overlap or intervening))
                        return IntegrationOutcome("stale", branch=ws.branch_name, overlap=hint)

        try:
            await wsm.merge_into(candidate, ws)  # 冲突于 candidate merge --abort,不碰真实 base
            self._metrics.incr("integration.merged")
            return IntegrationOutcome("integrated", branch=ws.branch_name)
        except GitWorktreeError as exc:
            self._metrics.incr("integration.conflicts")
            return IntegrationOutcome("stale", branch=ws.branch_name, conflict=str(exc))

    async def discard(self, worker: WorkerRun) -> None:
        """丢弃一个过期 Worker 的 worktree/分支（重跑前清理,避免泄漏）。"""
        if self._wsm is None:
            return
        try:
            await self._wsm.cleanup(worker.workspace, keep=False)
        except Exception:
            self._metrics.incr("integration.discard_failures")


def _norm(path: str) -> str:
    return unicodedata.normalize("NFKC", path).strip().replace("\\", "/").lstrip("./")


def _read_info(worker: WorkerRun) -> tuple[set[str], bool]:
    """从 Worker 的工具调用里提取读集;用过 run_command 则读集不可知（返回 unknown=True）。"""
    reads: set[str] = set()
    unknown = False
    for run in worker.run.context.tool_runs:
        name = run.call.name
        if name in _UNKNOWN_READ_TOOLS:
            unknown = True
        elif name in _READ_TOOLS:
            path = run.call.arguments.get("path")
            if isinstance(path, str) and path:
                reads.add(_norm(path))
    return reads, unknown


__all__ = ["IntegrationCoordinator", "IntegrationOutcome"]
