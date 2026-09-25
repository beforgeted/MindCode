from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
from codeagent.workspace.manager import LocalWorkspaceManager, build_workspace_manager

_HAS_GIT = shutil.which("git") is not None


def _init_repo(root: Path) -> None:
    def run(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)

    run("init")
    run("config", "user.email", "t@t.com")
    run("config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    run("add", ".")
    run("commit", "-m", "init")


def _commit_file(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", f"add {name}"], check=True, capture_output=True
    )


async def test_build_manager_falls_back_to_local_for_non_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    manager = await build_workspace_manager(plain, isolation="auto")
    assert isinstance(manager, LocalWorkspaceManager)
    assert manager.isolated is False
    ws = await manager.create("run_1")
    assert ws.is_isolated is False
    assert ws.root == plain.resolve()


async def test_worktree_isolation_requires_git_when_forced(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RuntimeError):
        await build_workspace_manager(plain, isolation="worktree")


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_git_worktree_create_and_cleanup(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    manager = await build_workspace_manager(repo, isolation="auto")
    assert isinstance(manager, GitWorktreeWorkspaceManager)
    assert manager.isolated is True

    ws = await manager.create("run_abc")
    assert ws.is_isolated is True
    assert ws.branch_name == "codeagent/run_abc"
    assert ws.root.exists()
    assert (ws.root / "seed.txt").exists()  # worktree 带着仓库内容

    await manager.cleanup(ws)
    assert not ws.root.exists()


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_git_worktree_merge_back_to_base(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    manager = await build_workspace_manager(repo, isolation="auto")
    ws = await manager.create("run_merge")
    # 在 worktree 分支上提交一处改动。
    _commit_file(ws.root, "worker.txt", "hello\n")
    await manager.merge(ws)
    assert (repo / "worker.txt").exists()  # 已合并回 base
    await manager.cleanup(ws)
