"""Non-Git recovery fault windows; memory domains are not container evidence."""
from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from codeagent.execution.publication import WorkspacePublication
from codeagent.llm.stub_client import StubLlmClient
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.run_store import AttemptState
from tests import test_snapshot_tasks as fixtures
from tests.test_snapshot_tasks import client, planner

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='POSIX snapshot recovery')


@pytest.fixture
def task(tmp_path, monkeypatch):
    return fixtures.make_task_fixture(tmp_path, monkeypatch)


class NoPlanner:
    async def plan(self, task):
        raise AssertionError('resume must use saved DAG')


async def resume(config, run_id, scripted=None):
    async with MasterSession(config, llm_client=scripted or StubLlmClient([]),
                             planner=NoPlanner()) as session:
        assert session.master is not None
        result = await session.master.run('resume', session_id=session.session.session_id,
                                          resume_master_run_id=run_id)
        return result, await session.master._run_store.load_run(run_id)


@pytest.mark.parametrize('window', ['before_write', 'before_db_ack', 'before_retire'])
async def test_verified_crash_windows_resume_without_workers(task, monkeypatch, window):
    root, config, manager = task
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        assert session.master is not None and session.master._snapshot_store is not None
        target = (session.master._wsm if window == 'before_write' else
                  session.master._snapshot_store if window == 'before_db_ack' else
                  WorkspacePublication)
        method = {'before_write': 'promote', 'before_db_ack': 'applied',
                  'before_retire': 'acknowledge'}[window]

        def fail(*args, **kwargs):
            raise OSError('injected handoff interruption')

        async def async_fail(*args, **kwargs):
            fail()

        with monkeypatch.context() as patch:
            patch.setattr(target, method, fail if window == 'before_retire' else async_fail)
            failed = await session.run_task('edit')
        assert not failed.integrated
    if window == 'before_write':
        assert (root / 'seed.txt').read_text() == 'user'
    else:
        assert (root / 'seed.txt').read_text() == 'candidate'
        # A subsequent editor update is not a reason to replay an applied publication.
        (root / 'seed.txt').write_text('editor-after-publish')
    manager.calls.clear()
    recovered, record = await resume(config, failed.master_run_id)
    assert recovered.integrated and recovered.scheduler is None and record is not None
    assert record.status == 'success' and record.last_attempt is not None
    assert record.last_attempt.state == AttemptState.PROMOTED
    assert not manager.calls
    assert (root / 'seed.txt').read_text() == (
        'candidate' if window == 'before_write' else 'editor-after-publish')
    assert not list(config.state_root.rglob('journal.json'))
    again, _ = await resume(config, failed.master_run_id)
    assert again.integrated and not manager.calls


async def test_unverified_attempt_restarts_saved_graph(task, monkeypatch):
    root, config, _ = task
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        assert session.master is not None

        async def fail(*args, **kwargs):
            raise OSError('verification interrupted')

        monkeypatch.setattr(session.master._verifier, 'verify', fail)
        failed = await session.run_task('edit')
    assert not failed.integrated and (root / 'seed.txt').read_text() == 'user'
    recovered, record = await resume(config, failed.master_run_id, client())
    assert recovered.integrated and record is not None and len(record.attempts) == 2
    assert record.attempts[0].state == AttemptState.DISCARDED
    assert (root / 'seed.txt').read_text() == 'candidate'


@pytest.mark.parametrize('damage', ['legacy', 'binding', 'blob', 'same_bytes_no_receipt'])
async def test_missing_or_mismatched_evidence_preserves_files_and_run(task, monkeypatch, damage):
    root, config, _ = task
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        assert session.master is not None

        async def fail(*args, **kwargs):
            raise OSError('before publish')

        monkeypatch.setattr(session.master._wsm, 'promote', fail)
        failed = await session.run_task('edit')
    with sqlite3.connect(config.state_root / 'runs.db') as db:
        if damage == 'legacy':
            db.execute('DELETE FROM snapshot_run')
        elif damage == 'binding':
            binding = json.loads(db.execute('SELECT binding FROM snapshot_run').fetchone()[0])
            binding['root'] = '/another-project'
            db.execute('UPDATE snapshot_run SET binding=?', (json.dumps(binding),))
        elif damage == 'blob':
            db.execute('UPDATE snapshot_run SET initial=?', (b'{}',))
        else:
            (root / 'seed.txt').write_text('candidate')
            (root / 'note.txt').write_text('accepted')
        before = db.execute('SELECT status, promoted_sha FROM master_run').fetchone()
    result, _ = await resume(config, failed.master_run_id)
    assert not result.integrated
    with sqlite3.connect(config.state_root / 'runs.db') as db:
        assert db.execute('SELECT status, promoted_sha FROM master_run').fetchone() == before
    assert (root / 'seed.txt').read_text() == (
        'candidate' if damage == 'same_bytes_no_receipt' else 'user')


async def test_pending_handoff_blocks_foreign_run(task, monkeypatch):
    _, config, _ = task
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        assert session.master is not None and session.master._snapshot_store is not None

        async def fail(*args, **kwargs):
            raise OSError('ack interrupted')

        monkeypatch.setattr(session.master._snapshot_store, 'applied', fail)
        failed = await session.run_task('edit')
    async with MasterSession(config, llm_client=client(), planner=planner()) as session:
        blocked = await session.run_task('another edit')
        assert not blocked.integrated and 'resume' in blocked.reason
    recovered, _ = await resume(config, failed.master_run_id)
    assert recovered.integrated
