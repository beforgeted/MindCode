"""Phase 0 A2：post-promote 执行被延后的外部动作（审批通过才执行）。"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.orchestration.global_verifier import NoFailureVerifier
from codeagent.orchestration.master_runtime import MasterRuntime
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.tool.approval import AllowExternalApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.deferred import DeferredAction
from codeagent.tool.effects import EffectKind, RetryPolicy
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager

_HAS_GIT = shutil.which("git") is not None


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for a in (["init"], ["config", "user.email", "t@t"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    (repo / "s.txt").write_text("s", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "i"], check=True, capture_output=True)
    return repo


def _master(repo: Path, approval) -> MasterRuntime:
    wsm = GitWorktreeWorkspaceManager(repo, repo / ".home" / "wt")
    return MasterRuntime(
        planner=StaticPlanner(TaskGraph([Step("s", "default", "x")])),
        scheduler=cast(StepScheduler, cast(Any, object())),  # _execute_deferred 不用它
        global_verifier=NoFailureVerifier(),
        workspace_manager=wsm,
        approval_policy=approval,
        artifact_store=FileArtifactStore(repo / ".home"),
    )


def _action(cmd: str) -> DeferredAction:
    return DeferredAction(
        command=cmd, effect=EffectKind.EXTERNAL_SIDE_EFFECT,
        retry=RetryPolicy.NEVER, reason="外部副作用",
    )


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_deferred_executed_when_approved(tmp_path: Path):
    repo = _repo(tmp_path)
    mr = _master(repo, AllowExternalApprovalPolicy())
    ex, fa, sk = await mr._execute_deferred([_action("echo ran > marker.txt")], None)
    assert (ex, fa, sk) == (1, 0, 0)
    assert "ran" in (repo / "marker.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_deferred_skipped_when_denied(tmp_path: Path):
    repo = _repo(tmp_path)
    mr = _master(repo, DenyExternalApprovalPolicy())
    ex, fa, sk = await mr._execute_deferred([_action("echo nope > marker.txt")], None)
    assert (ex, fa, sk) == (0, 0, 1)
    assert not (repo / "marker.txt").exists()  # 未批准 → 不执行


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_deferred_idempotent_by_id(tmp_path: Path):
    repo = _repo(tmp_path)
    mr = _master(repo, AllowExternalApprovalPolicy())
    action = _action("echo x >> counter.txt")
    ex, _, _ = await mr._execute_deferred([action, action], None)  # 同 id 两次
    assert ex == 1  # 只执行一次
