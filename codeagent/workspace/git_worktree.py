"""GitWorktreeWorkspaceManager：Worker worktree + Master Attempt candidate 事务。

所有 git 调用走 asyncio.to_thread（子进程阻塞 I/O，留在事件循环里会卡住并发 AgentRun）。

Master Attempt Transaction（P6）：
- 每次 Attempt 建一个 **candidate** 分支/worktree（从 original_base 切）。
- Worker worktree 从 **candidate HEAD** 切；增量集成 merge 进 **candidate**，不碰真实 base。
- 验收在独立 **validation worktree**（detached @candidate_sha）跑，避免测试副作用改动被验证的 tree。
- 验收通过才 `promote`：CAS（真实 base HEAD 仍等于 original_base）成立时 `merge --ff-only`。
- reject/失败：`discard`（回收 worktree+branch），下一 Attempt 从 original_base 重开。
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

    async def base_revision(self) -> str:
        """真实 base 当前 HEAD（一次 MasterRun 开始时固定为 original_base）。"""
        return await asyncio.to_thread(_run_git, self._repo, "rev-parse", "HEAD")

    async def head(self, root: Path | None = None) -> str:
        """某个 worktree（默认真实 repo）的 HEAD sha。"""
        return await asyncio.to_thread(_run_git, root or self._repo, "rev-parse", "HEAD")

    async def changed_files(self, from_rev: str, to_rev: str) -> set[str]:
        """from_rev..to_rev 之间改动的文件（object db 共享,从 repo 查即可）。"""
        return await asyncio.to_thread(self._diff_names, from_rev, to_rev)

    async def branch_files(self, base_rev: str, branch: str) -> set[str]:
        """branch 相对 base_rev 改动的文件（Worker 写集,权威）。"""
        return await asyncio.to_thread(self._diff_names, base_rev, branch)

    def _diff_names(self, a: str, b: str) -> set[str]:
        try:
            out = _run_git(self._repo, "diff", "--name-only", a, b)
        except GitWorktreeError:
            return set()
        return {line.strip() for line in out.splitlines() if line.strip()}

    async def diff_text(self, a: str, b: str, *, max_bytes: int = 20_000) -> str:
        out = await asyncio.to_thread(self._diff_text, a, b)
        return out[:max_bytes]

    def _diff_text(self, a: str, b: str) -> str:
        try:
            return _run_git(self._repo, "diff", a, b)
        except GitWorktreeError:
            return ""

    async def run_check(
        self, root: Path, command: str, *, timeout_s: float = 300.0
    ) -> tuple[int, str]:
        """在给定 worktree 里跑验收命令,返回 (exit_code, 合并输出)。用于 validation worktree。"""
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=str(root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError:
            proc.kill()
            return 124, "验收命令超时"
        return proc.returncode or 0, (out or b"").decode("utf-8", errors="replace")[-8000:]

    # ---- candidate（Attempt 作用域）----

    async def create_candidate(self, base_rev: str) -> WorkspaceContext:
        branch = f"codeagent/cand/{new_id('att')}"
        wt_id = new_id("cand")
        path = self._dir / wt_id
        await asyncio.to_thread(self._add_worktree, path, branch, base_rev)
        return WorkspaceContext(
            root=path.resolve(),
            worktree_id=wt_id,
            branch_name=branch,
            is_isolated=True,
            base_revision=base_rev,
        )

    # ---- Worker（Step 作用域，从 candidate HEAD 切）----

    async def create(self, run_id: str, *, base_ref: str | None = None) -> WorkspaceContext:
        worktree_id = new_id("wt")
        branch = f"codeagent/{run_id}"
        path = self._dir / worktree_id
        ref = base_ref or "HEAD"
        base_revision = await asyncio.to_thread(self._create_worker, path, branch, ref)
        return WorkspaceContext(
            root=path.resolve(),
            worktree_id=worktree_id,
            branch_name=branch,
            is_isolated=True,
            base_revision=base_revision,
        )

    def _create_worker(self, path: Path, branch: str, ref: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        base_revision = _run_git(self._repo, "rev-parse", ref)
        _run_git(self._repo, "worktree", "add", "-b", branch, str(path), base_revision)
        return base_revision

    def _add_worktree(self, path: Path, branch: str, ref: str) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        rev = _run_git(self._repo, "rev-parse", ref)
        _run_git(self._repo, "worktree", "add", "-b", branch, str(path), rev)
        return rev

    # ---- 回收 ----

    async def cleanup(self, workspace: WorkspaceContext, *, keep: bool = False) -> None:
        if not workspace.is_isolated or keep:
            return
        await asyncio.to_thread(self._remove_worktree, workspace.root, workspace.branch_name)

    def _remove_worktree(self, root: Path, branch: str | None) -> None:
        try:
            _run_git(self._repo, "worktree", "remove", "--force", str(root))
        except GitWorktreeError:
            shutil.rmtree(root, ignore_errors=True)
            try:
                _run_git(self._repo, "worktree", "prune")
            except GitWorktreeError:
                pass
        if branch:
            try:
                _run_git(self._repo, "branch", "-D", branch)
            except GitWorktreeError:
                pass

    # ---- commit / merge（进 candidate，不碰真实 base）----

    async def commit(
        self, workspace: WorkspaceContext, *, message: str = "codeagent worker changes"
    ) -> bool:
        if not workspace.is_isolated:
            return False
        return await asyncio.to_thread(self._commit_sync, workspace, message)

    def _commit_sync(self, workspace: WorkspaceContext, message: str) -> bool:
        # 排除运行期产物（跑测试生成的 __pycache__/*.pyc），不混进集成。
        _run_git(
            workspace.root, "add", "-A", "--", ".", ":(exclude)*.pyc", ":(exclude)*__pycache__*"
        )
        if not _run_git(workspace.root, "diff", "--cached", "--name-only").strip():
            return False
        _run_git(workspace.root, "commit", "-m", message)
        return True

    async def merge_into(self, candidate: WorkspaceContext, worker: WorkspaceContext) -> None:
        """把 Worker 分支合并进 candidate worktree。冲突时于 candidate `merge --abort`。"""
        if not worker.branch_name:
            return
        await asyncio.to_thread(self._merge_sync, candidate.root, worker.branch_name)

    def _merge_sync(self, target_root: Path, branch: str) -> None:
        try:
            _run_git(target_root, "merge", "--no-edit", branch)
        except GitWorktreeError:
            try:
                _run_git(target_root, "merge", "--abort")
            except GitWorktreeError:
                pass
            raise

    # ---- validation worktree（detached @candidate_sha，验收在此跑）----

    async def create_validation(self, candidate_sha: str) -> WorkspaceContext:
        wt_id = new_id("val")
        path = self._dir / wt_id
        await asyncio.to_thread(self._add_detached, path, candidate_sha)
        return WorkspaceContext(
            root=path.resolve(), worktree_id=wt_id, branch_name=None,
            is_isolated=True, base_revision=candidate_sha,
        )

    def _add_detached(self, path: Path, sha: str) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        _run_git(self._repo, "worktree", "add", "--detach", str(path), sha)

    async def has_uncommitted(self, workspace: WorkspaceContext) -> bool:
        """validation worktree 是否有未跟踪/未提交改动（测试生成物依赖 → 不应放行）。"""
        out = await asyncio.to_thread(
            _run_git, workspace.root, "status", "--porcelain", "--untracked-files=all"
        )
        return bool(out.strip())

    # ---- promote：CAS 原子推进真实 base ----

    async def promote(self, candidate_sha: str, *, expected_base: str) -> bool:
        """真实 base HEAD 仍等于 expected_base 才 `merge --ff-only candidate_sha`。

        返回 False 表示 base 已被外部推进（BASE_STALE）,不强推。
        """
        return await asyncio.to_thread(self._promote_sync, candidate_sha, expected_base)

    def _promote_sync(self, candidate_sha: str, expected_base: str) -> bool:
        actual = _run_git(self._repo, "rev-parse", "HEAD")
        if actual != expected_base:
            return False
        _run_git(self._repo, "merge", "--ff-only", candidate_sha)
        return True
