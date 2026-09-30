"""Phase 0 A2：post-promote 执行被延后的外部动作（审批通过才执行）。"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.orchestration.global_verifier import NoFailureVerifier
from codeagent.orchestration.master_runtime import MasterRuntime
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.run_store import AttemptRecord, AttemptState, SqliteRunStore
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.tool.approval import AllowExternalApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.deferred import DeferredAction, DeferredRecord, DeferredState
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


def _master(repo: Path, approval, store=None) -> MasterRuntime:
    wsm = GitWorktreeWorkspaceManager(repo, repo / ".home" / "wt")
    return MasterRuntime(
        planner=StaticPlanner(TaskGraph([Step("s", "default", "x")])),
        scheduler=cast(StepScheduler, cast(Any, object())),  # _execute_deferred 不用它
        global_verifier=NoFailureVerifier(),
        workspace_manager=wsm,
        approval_policy=approval,
        artifact_store=FileArtifactStore(repo / ".home"),
        run_store=store,
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
    records = await mr._execute_deferred(
        (DeferredRecord(_action("echo ran > marker.txt")),), None,
        master_run_id="m", attempt_no=1,
    )
    assert records[0].state == DeferredState.SUCCEEDED
    assert "ran" in (repo / "marker.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_deferred_skipped_when_denied(tmp_path: Path):
    repo = _repo(tmp_path)
    mr = _master(repo, DenyExternalApprovalPolicy())
    records = await mr._execute_deferred(
        (DeferredRecord(_action("echo nope > marker.txt")),), None,
        master_run_id="m", attempt_no=1,
    )
    assert records[0].state == DeferredState.SKIPPED
    assert not (repo / "marker.txt").exists()  # 未批准 → 不执行


@pytest.mark.skipif(not _HAS_GIT, reason="git 不可用")
async def test_deferred_idempotent_by_id(tmp_path: Path):
    repo = _repo(tmp_path)
    mr = _master(repo, AllowExternalApprovalPolicy())
    action = _action("echo x >> counter.txt")
    records = await mr._execute_deferred(
        (DeferredRecord(action), DeferredRecord(action)), None,
        master_run_id="m", attempt_no=1,
    )
    assert len(records) == 1
    assert (repo / "counter.txt").read_text().split() == ["x"]


async def _saved_run(repo, actions, *, state=AttemptState.PROMOTED):
    store = SqliteRunStore(repo / ".home" / "runs.db")
    await store.start()
    head = (await asyncio.to_thread(subprocess.run,
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )).stdout.strip()
    await store.save_run(
        master_run_id="m", session_id="s", task="t", status="running",
        graph=TaskGraph([Step("s", "default", "x")]), original_base_sha=head,
    )
    await store.save_attempt("m", AttemptRecord(
        1, state=state, candidate_sha=head, original_base_sha=head,
    ))
    await store.save_deferred("m", 1, actions)
    return store


@pytest.mark.parametrize("state", [AttemptState.PROMOTED, AttemptState.PROMOTING, "success"])
async def test_resume_executes_pending_and_never_repeats_success(tmp_path, state):
    repo = _repo(tmp_path)
    action = _action("echo once >> counter.txt")
    store = await _saved_run(repo, [action], state=state)
    if state == "success":
        await store.update_run_status("m", "success")
    # discarded Attempt 的动作不能混进成功 Attempt。
    await store.save_deferred("m", 0, [_action("echo wrong > wrong.txt")])
    for _ in range(2):
        reopened = SqliteRunStore(repo / ".home" / "runs.db")
        await reopened.start()
        master = _master(repo, AllowExternalApprovalPolicy(), reopened)
        final = await master.run("", session_id="s", resume_master_run_id="m")
        assert final.integrated and final.deferred_executed == 1
    assert (repo / "counter.txt").read_text().split() == ["once"]
    assert not (repo / "wrong.txt").exists()


async def test_crash_after_effect_before_result_is_unknown_not_replayed(tmp_path):
    repo = _repo(tmp_path)
    action = _action("echo once >> counter.txt")
    store = await _saved_run(repo, [action])
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    original = store.update_deferred

    async def crash_on_success(master_run_id, attempt_no, record):
        if record.state == DeferredState.SUCCEEDED:
            raise RuntimeError("simulated result persistence failure")
        await original(master_run_id, attempt_no, record)

    store.update_deferred = crash_on_success
    with pytest.raises(RuntimeError, match="persistence failure"):
        await master.run("", session_id="s", resume_master_run_id="m")
    reopened = SqliteRunStore(repo / ".home" / "runs.db")
    await reopened.start()
    master = _master(repo, AllowExternalApprovalPolicy(), reopened)
    for _ in range(2):
        final = await master.run("", session_id="s", resume_master_run_id="m")
        assert final.deferred_unknown == 1 and final.deferred_executed == 0
    assert (repo / "counter.txt").read_text().split() == ["once"]


@pytest.mark.parametrize("attempts, expected", [(1, DeferredState.SUCCEEDED),
                                               (2, DeferredState.UNKNOWN)])
async def test_idempotent_resume_respects_persisted_budget(tmp_path, attempts, expected):
    repo = _repo(tmp_path)
    action = replace(_action("echo value > value.txt"), retry=RetryPolicy.IDEMPOTENT)
    store = await _saved_run(repo, [action])
    await store.update_deferred("m", 1, DeferredRecord(action, DeferredState.RUNNING, attempts))
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    final = await master.run("", session_id="s", resume_master_run_id="m")
    assert final.deferred_records[0].state == expected
    assert final.deferred_records[0].attempts == 2
    assert (repo / "value.txt").exists() == (attempts == 1)


async def test_start_persistence_failure_prevents_command(tmp_path):
    repo = _repo(tmp_path)
    action = _action("echo bad > marker.txt")
    store = await _saved_run(repo, [action])

    async def fail(master_run_id, attempt_no, record):
        raise RuntimeError("storage unavailable")

    store.update_deferred = fail
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    with pytest.raises(RuntimeError, match="storage unavailable"):
        await master.run("", session_id="s", resume_master_run_id="m")
    assert not (repo / "marker.txt").exists()


async def test_failed_and_denied_actions_remain_terminal_on_resume(tmp_path):
    repo = _repo(tmp_path)
    action = replace(_action("exit 1"), retry=RetryPolicy.IDEMPOTENT)
    store = await _saved_run(repo, [action])
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    for _ in range(2):
        final = await master.run("", session_id="s", resume_master_run_id="m")
        assert final.deferred_failed == 1
        assert final.deferred_records[0].attempts == 2
    denied = _action("echo no > denied.txt")
    await store.save_deferred("m", 1, [denied])
    master = _master(repo, DenyExternalApprovalPolicy(), store)
    final = await master.run("", session_id="s", resume_master_run_id="m")
    assert final.deferred_skipped == 1
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    final = await master.run("", session_id="s", resume_master_run_id="m")
    assert final.deferred_skipped == 1
    assert not (repo / "denied.txt").exists()
