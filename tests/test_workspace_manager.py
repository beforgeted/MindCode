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
async def test_git_candidate_merge_and_promote(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    manager = await build_workspace_manager(repo, isolation="auto")
    assert isinstance(manager, GitWorktreeWorkspaceManager)
    original = await manager.base_revision()
    # candidate 从 original 切;Worker 从 candidate 切、提交、并回 candidate。
    candidate = await manager.create_candidate(original)
    ws = await manager.create("run_merge", base_ref=candidate.branch_name)
    _commit_file(ws.root, "worker.txt", "hello\n")
    await manager.merge_into(candidate, ws)
    # 此时真实 base 尚未变化。
    assert not (repo / "worker.txt").exists()
    assert await manager.base_revision() == original
    # 冻结 candidate 并 CAS 推进真实 base。
    candidate_sha = await manager.head(candidate.root)
    assert await manager.promote(candidate_sha, expected_base=original) is True
    assert (repo / "worker.txt").exists()
    await manager.cleanup(ws)
    await manager.cleanup(candidate)


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_promote_refuses_when_base_moved(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    manager = await build_workspace_manager(repo, isolation="auto")
    assert isinstance(manager, GitWorktreeWorkspaceManager)
    original = await manager.base_revision()
    candidate = await manager.create_candidate(original)
    ws = await manager.create("run_x", base_ref=candidate.branch_name)
    _commit_file(ws.root, "worker.txt", "hi\n")
    await manager.merge_into(candidate, ws)
    candidate_sha = await manager.head(candidate.root)
    # 验证期间真实 base 被外部推进 → CAS 应安全拒绝,不覆盖外部提交。
    _commit_file(repo, "external.txt", "outside\n")
    assert await manager.promote(candidate_sha, expected_base=original) is False
    assert not (repo / "worker.txt").exists()  # 未强推
    assert (repo / "external.txt").exists()  # 外部提交保留
    await manager.cleanup(ws)
    await manager.cleanup(candidate)
