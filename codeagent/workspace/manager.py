"""WorkspaceManager：为并行 Worker 分配 / 回收工作区。

auto 策略：root 是 git 工作树 → GitWorktreeWorkspaceManager（每个 Worker 独立
worktree + 分支，并行写互不干扰）；否则 → LocalWorkspaceManager（共享 root，
由 StepScheduler 用写锁把写操作串行化）。

cleanup 的所有权在 MasterRuntime（合并之后），不在 AgentRuntime —— 否则成功路径上
worktree 会在被合并之前就删掉（并行文档 §8 vs §7 的坑，见 workspace/context.py 注释）。
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Protocol, runtime_checkable

from codeagent.workspace.context import WorkspaceContext


@runtime_checkable
class WorkspaceManager(Protocol):
    @property
    def isolated(self) -> bool: ...

    async def create(self, run_id: str) -> WorkspaceContext: ...

    async def cleanup(self, workspace: WorkspaceContext, *, keep: bool = False) -> None: ...


class LocalWorkspaceManager:
    """非隔离：所有 Worker 共享同一个 root。写并发由上层写锁串行化。"""

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root).resolve()

    @property
    def isolated(self) -> bool:
        return False

    async def create(self, run_id: str) -> WorkspaceContext:
        return WorkspaceContext.local(self._root)

    async def cleanup(self, workspace: WorkspaceContext, *, keep: bool = False) -> None:
        return None


def _is_git_worktree(root: Path) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


async def build_workspace_manager(
    root: Path | str,
    *,
    isolation: str = "auto",
    worktree_root: Path | str | None = None,
) -> WorkspaceManager:
    """isolation: "auto" | "worktree" | "local"。auto 时按 git 可用性选择。"""
    root_path = Path(root)
    if isolation == "local":
        return LocalWorkspaceManager(root_path)
    is_git = await asyncio.to_thread(_is_git_worktree, root_path)
    if isolation == "worktree" and not is_git:
        raise RuntimeError(f"isolation=worktree 需要 git 工作树，但 {root_path} 不是")
    if is_git:
        from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager

        wt_root = Path(worktree_root) if worktree_root else root_path / ".codeagent"
        return GitWorktreeWorkspaceManager(root_path, wt_root / "worktrees")
    return LocalWorkspaceManager(root_path)
