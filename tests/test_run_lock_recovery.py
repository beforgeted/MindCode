from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from codeagent.orchestration.run_lock import RunLeaseManager, RunLockBusy
from codeagent.orchestration.run_store import AttemptRecord, AttemptState, SqliteRunStore
from codeagent.tool.approval import AllowExternalApprovalPolicy
from codeagent.tool.deferred import DeferredState
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
from tests.test_deferred_execution import _action, _master, _repo, _saved_run


async def test_second_resume_cannot_read_or_repeat_active_external_action(tmp_path):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [_action('echo once >> counter.txt')])
    entered, release = asyncio.Event(), asyncio.Event()
    class Gate:
        async def approve(self, decision, *, command):
            entered.set()
            await release.wait()
            return True
    first = _master(repo, Gate(), store)
    second_store = SqliteRunStore(repo / '.home/runs.db')
    await second_store.start()
    second = _master(repo, AllowExternalApprovalPolicy(), second_store)
    original_load = second_store.load_run
    async def must_not_read(master_run_id):
        pytest.fail('busy contender must not read recovery state')
    second_store.load_run = must_not_read
    owner = asyncio.create_task(first.run('', session_id='a', resume_master_run_id='m'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        before = await store.load_run('m')
        assert before is not None
        blocked = await second.run('', session_id='b', resume_master_run_id='m')
        assert not blocked.integrated and '另一个' in blocked.reason
        after = await store.load_run('m')
        assert after is not None and after.status == before.status
        assert after.attempts == before.attempts and after.outcomes == before.outcomes
        assert not (repo / 'counter.txt').exists()
        release.set()
        assert (await owner).deferred_executed == 1
        second_store.load_run = original_load
        final = await second.run('', session_id='b', resume_master_run_id='m')
        assert final.integrated and final.deferred_records[0].state == DeferredState.SUCCEEDED
        assert (repo / 'counter.txt').read_text().split() == ['once']
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_new_run_holds_lease_before_planning_and_releases_on_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    store = SqliteRunStore(repo / '.home/runs.db')
    await store.start()
    first = _master(repo, AllowExternalApprovalPolicy(), store)
    entered, release = asyncio.Event(), asyncio.Event()
    class Planner:
        async def plan(self, task):
            entered.set()
            await release.wait()
            raise RuntimeError('planning failed')
    first._planner = Planner()
    monkeypatch.setattr('codeagent.orchestration.master_runtime.new_id', lambda prefix: 'new')
    second_store = SqliteRunStore(repo / '.home/runs.db')
    await second_store.start()
    second = _master(repo, AllowExternalApprovalPolicy(), second_store)
    owner = asyncio.create_task(first.run('new task', session_id='a'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        blocked = await second.run('', session_id='b', resume_master_run_id='new')
        assert not blocked.integrated
        release.set()
        with pytest.raises(RuntimeError, match='planning failed'):
            await owner
        # An exception releases ownership; a subsequent lookup reaches missing-record semantics.
        with pytest.raises(ValueError, match='找不到'):
            await second.run('', session_id='b', resume_master_run_id='new')
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_export_is_inside_lease_and_busy_contender_does_not_export(tmp_path):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [])
    first = _master(repo, AllowExternalApprovalPolicy(), store)
    entered, release = asyncio.Event(), asyncio.Event()
    class Exporter:
        async def export(self, master_run_id: str, *, session_snapshot: dict | None = None) -> Path:
            entered.set()
            await release.wait()
            return tmp_path / 'report.json'
    first._trajectory_exporter = Exporter()
    second = _master(repo, AllowExternalApprovalPolicy(), SqliteRunStore(repo / '.home/runs.db'))
    class Unexpected:
        async def export(self, master_run_id: str, *, session_snapshot: dict | None = None) -> Path:
            pytest.fail('busy contender must not export')
    second._trajectory_exporter = Unexpected()
    owner = asyncio.create_task(first.run('', session_id='a', resume_master_run_id='m'))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert not (await second.run('', session_id='b', resume_master_run_id='m')).integrated
        release.set()
        assert (await owner).integrated
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_recovery_reclaims_only_recorded_run_resources(tmp_path):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [], state=AttemptState.RUNNING)
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    git = cast(GitWorktreeWorkspaceManager, master._wsm)
    head = await git.base_revision()
    owned = await git.create_candidate(head)
    unrelated = await git.create_candidate(head)
    other_worker = await git.create('unrelated')
    record = await store.load_run('m')
    assert record is not None
    record = replace(record, attempts=(AttemptRecord(1, candidate_branch=owned.branch_name),))
    await master._reclaim_orphans(record, git)
    assert not owned.root.exists()
    assert unrelated.root.exists() and other_worker.root.exists()
    assert await git.head(unrelated.root) == head
    await git.cleanup(unrelated, keep=False)
    await git.cleanup(other_worker, keep=False)


async def test_lock_error_preserves_run_state_and_never_starts_recovery(tmp_path):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [])
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    file = tmp_path / 'file'
    file.write_text('keep')
    master._run_leases = RunLeaseManager(file / 'locks')
    before = await store.load_run('m')
    assert before is not None
    result = await master.run('', session_id='a', resume_master_run_id='m')
    assert not result.integrated and '执行权' in result.reason
    after = await store.load_run('m')
    assert after is not None and after.status == before.status
    assert file.read_text() == 'keep'


@pytest.mark.parametrize('end', ['normal', 'kill'])
async def test_actual_two_process_resumes_and_crash_takeover(tmp_path, end):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [_action('echo once >> counter.txt')])
    before = await store.load_run('m')
    assert before is not None
    argv = [sys.executable, '-m', 'tests.run_recovery_probe', str(repo)]
    owner = await asyncio.to_thread(
        subprocess.Popen, [*argv, 'hold'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    async def contender():
        result = await asyncio.to_thread(subprocess.run, [*argv, 'fast'],
                                         capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    try:
        async with asyncio.timeout(15):
            while not (repo / 'gate-owned').exists():
                assert owner.poll() is None
                await asyncio.sleep(0.01)
        # The owner may legitimately supplement success before reaching approval.
        # Compare the contender against the state after the owner reaches its gate.
        before = await store.load_run('m')
        assert before is not None
        blocked = await contender()
        assert not blocked['integrated'] and '另一个' in blocked['reason']
        assert not (repo / 'counter.txt').exists()
        after = await store.load_run('m')
        assert after is not None and after.status == before.status
        if end == 'kill':
            owner.kill()
        else:
            (repo / 'gate-release').touch()
        output, error = await asyncio.to_thread(owner.communicate, timeout=15)
        if end == 'normal':
            assert owner.returncode == 0, error
            assert json.loads(output)['executed'] == 1
        final = await contender()
        assert final['integrated'] and final['executed'] == 1
        assert (repo / 'counter.txt').read_text().split() == ['once']
    finally:
        if owner.poll() is None:
            owner.kill()
        await asyncio.to_thread(owner.communicate, timeout=15)


async def test_cancel_does_not_release_run_while_sqlite_writer_is_still_active(tmp_path):
    repo = _repo(tmp_path)
    store = await _saved_run(repo, [])
    master = _master(repo, AllowExternalApprovalPolicy(), store)
    entered, release = threading.Event(), threading.Event()
    original = store._with_connection
    def slow(operation):
        entered.set()
        assert release.wait(10)
        return original(operation)
    store._with_connection = slow
    owner = asyncio.create_task(master.run('', session_id='a', resume_master_run_id='m'))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()  # A second cancellation must not detach the original thread.
        await asyncio.sleep(0)
        assert not owner.done()
        with pytest.raises(RunLockBusy), RunLeaseManager(store.run_lock_directory).acquire('m'):
            pass
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        with RunLeaseManager(store.run_lock_directory).acquire('m'):
            pass
    finally:
        release.set()
        if not owner.done():
            owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)
