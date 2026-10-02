"""RunStore snapshot integrity and publication binding, on both host platforms."""
from __future__ import annotations

import sqlite3
from dataclasses import replace

import pytest

from codeagent.execution.publication import PublicationUncertain
from codeagent.execution.snapshot import (
    SnapshotEntry,
    SnapshotLimits,
    TreeSnapshot,
    encode_snapshot,
)
from codeagent.orchestration.run_store import (
    AttemptRecord,
    AttemptState,
    RunStoreError,
    SqliteRunStore,
)
from codeagent.orchestration.snapshot_store import SnapshotRunStore, snapshot_revision
from codeagent.orchestration.task_graph import Step, TaskGraph


@pytest.fixture
async def checkpoint(tmp_path):
    db = SqliteRunStore(tmp_path / 'runs.db')
    await db.start()
    limits = SnapshotLimits()
    snapshots = SnapshotRunStore(db, limits)
    raw = encode_snapshot(TreeSnapshot((SnapshotEntry('seed.txt', b'user'),)), limits)
    revision = snapshot_revision(raw)
    await db.save_run(master_run_id='run', session_id='session', task='edit',
                      graph=TaskGraph([Step('one', 'default', 'edit')]),
                      status='running', original_base_sha=revision)
    await snapshots.initialize('run', {'root': '/project'}, raw)
    await db.save_attempt('run', AttemptRecord(1, AttemptState.RUNNING))
    yield db, snapshots, raw, revision


async def test_original_and_latest_frozen_are_persisted(checkpoint):
    db, store, raw, revision = checkpoint
    await store.freeze('run', 1, raw)
    loaded = await store.load('run')
    assert loaded is not None and loaded.initial == loaded.candidate == raw
    assert loaded.original_revision == loaded.candidate_revision == revision
    assert loaded.candidate_attempt == 1
    with sqlite3.connect(db._path) as connection:
        assert connection.execute('SELECT COUNT(*) FROM snapshot_run').fetchone() == (1,)


@pytest.mark.parametrize('phase', [AttemptState.RUNNING, AttemptState.VERIFYING,
                                   AttemptState.VERIFIED, AttemptState.DISCARDED])
async def test_only_promoting_candidate_can_prepare(checkpoint, phase):
    db, store, raw, revision = checkpoint
    await store.freeze('run', 1, raw)
    await db.update_attempt('run', 1, state=phase, candidate_sha=revision)
    with pytest.raises(RunStoreError):
        await store.prepare('run', 1, revision)


async def test_ack_is_bound_and_idempotent(checkpoint):
    db, store, raw, revision = checkpoint
    await store.freeze('run', 1, raw)
    await db.update_attempt('run', 1, state=AttemptState.PROMOTING, candidate_sha=revision)
    state = await store.prepare('run', 1, revision)
    again = await store.prepare('run', 1, revision)
    assert state == again and state.receipt_id is not None
    with pytest.raises(RunStoreError):
        await store.applied('run', replace(state, receipt_id='0' * 32))
    await store.applied('run', state)
    before = await db.load_run('run')
    await store.applied('run', state)
    after = await db.load_run('run')
    assert before is not None and after is not None
    assert before.status == after.status and before.attempts == after.attempts
    assert before.graph.steps == after.graph.steps
    with pytest.raises(RunStoreError):
        await store.freeze('run', 2, raw)


@pytest.mark.parametrize('column,value', [
    ('original_revision', 'snapshot:' + '0' * 64),
    ('initial', b'{}'), ('publication_state', 'invented'),
    ('publication_state', 'applied'),
])
async def test_corrupt_checkpoint_is_rejected(checkpoint, column, value):
    db, store, _, _ = checkpoint
    with sqlite3.connect(db._path) as connection:
        connection.execute(f'UPDATE snapshot_run SET {column}=?', (value,))
    with pytest.raises((RunStoreError, PublicationUncertain, ValueError)):
        await store.load('run')
