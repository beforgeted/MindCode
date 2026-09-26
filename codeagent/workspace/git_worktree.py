"""GitWorktreeWorkspaceManager：每个 Worker 一个 git worktree + 分支。

所有 git 调用走 asyncio.to_thread —— 子进程是阻塞 I/O，留在事件循环里会卡住
所有并发 AgentRun（V1 §6.1）。合并由 MasterRuntime 负责，这里只管建/删 worktree。
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

from codeagent.infra.ids import new_id
from codeagent.workspace.context import WorkspaceContext


class GitWorktreeError(RuntimeError):
    pass


def _run_git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise GitWorktreeError(
            f"git {' '.join(args)} 失败 (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout.strip()


class GitWorktreeWorkspaceManager:
    def __init__(self, repo_root: Path, worktrees_dir: Path) -> None:
        self._repo = Path(repo_root).resolve()
        self._dir = Path(worktrees_dir).resolve()

    @property
    def isolated(self) -> bool:
        return True

    @property
    def repo_root(self) -> Path:
        return self._repo

    def base_branch(self) -> str:
        return _run_git(self._repo, "rev-parse", "--abbrev-ref", "HEAD")

    async def create(self, run_id: str) -> WorkspaceContext:
        worktree_id = new_id("wt")
        branch = f"codeagent/{run_id}"
        path = self._dir / worktree_id
        await asyncio.to_thread(self._create_sync, path, branch)
        return WorkspaceContext(
            root=path.resolve(),
            worktree_id=worktree_id,
            branch_name=branch,
            is_isolated=True,
        )

    def _create_sync(self, path: Path, branch: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _run_git(self._repo, "worktree", "add", "-b", branch, str(path), "HEAD")

    async def cleanup(self, workspace: WorkspaceContext, *, keep: bool = False) -> None:
        if not workspace.is_isolated or keep:
            return
        await asyncio.to_thread(self._cleanup_sync, workspace)

    def _cleanup_sync(self, workspace: WorkspaceContext) -> None:
        try:
            _run_git(self._repo, "worktree", "remove", "--force", str(workspace.root))
        except GitWorktreeError:
            # worktree 已被移除或路径异常：兜底直接删目录 + prune。
            shutil.rmtree(workspace.root, ignore_errors=True)
            try:
                _run_git(self._repo, "worktree", "prune")
            except GitWorktreeError:
                pass
        if workspace.branch_name:
            try:
                _run_git(self._repo, "branch", "-D", workspace.branch_name)
            except GitWorktreeError:
                pass

    async def commit(
        self, workspace: WorkspaceContext, *, message: str = "codeagent worker changes"
    ) -> bool:
        """把 worktree 工作区的改动提交到它的分支。无改动返回 False。

        隔离模式下 Worker 的文件改动只在自己的 worktree 里，必须先提交到分支，
        MasterRuntime 才能通过 merge 把它带回 base。
        """
        if not workspace.is_isolated:
            return False
        return await asyncio.to_thread(self._commit_sync, workspace, message)

    def _commit_sync(self, workspace: WorkspaceContext, message: str) -> bool:
        # 排除 __pycache__/*.pyc 等运行期产物：Worker 跑测试会生成它们，
        # 不应混进合并回 base 的改动里。
        _run_git(
            workspace.root,
            "add",
            "-A",
            "--",
            ".",
            ":(exclude)*.pyc",
            ":(exclude)*__pycache__*",
        )
        staged = _run_git(workspace.root, "diff", "--cached", "--name-only")
        if not staged.strip():
            return False
        _run_git(workspace.root, "commit", "-m", message)
        return True

    async def merge(self, workspace: WorkspaceContext) -> None:
        """把 Worker 分支合并回 base。冲突时先 `git merge --abort` 保持 base 干净，再抛。"""
        if not workspace.branch_name:
            return
        await asyncio.to_thread(self._merge_sync, workspace.branch_name)

    def _merge_sync(self, branch: str) -> None:
        try:
            _run_git(self._repo, "merge", "--no-edit", branch)
        except GitWorktreeError:
            # 冲突/失败会把 base 工作树丢在半完成的 merge 状态（MERGE_HEAD + 冲突标记），
            # 导致 base 直接不可用。必须回滚,让 base 保持一致,再把冲突上抛交人工。
            try:
                _run_git(self._repo, "merge", "--abort")
            except GitWorktreeError:
                pass
            raise
