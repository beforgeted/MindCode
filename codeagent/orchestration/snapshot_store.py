"""Non-Git recovery checkpoints and publication receipts in the existing RunStore."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from codeagent.execution.snapshot import SnapshotLimits, decode_snapshot, encode_snapshot
from codeagent.orchestration.run_store import (
    AttemptState,
    RunStoreError,
    SqliteRunStore,
    _append_transition,
)


def snapshot_revision(raw: bytes) -> str:
    return 'snapshot:' + hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class SnapshotCheckpoint:
    binding: dict
    initial: bytes
    original_revision: str
    candidate: bytes | None = None
    candidate_revision: str | None = None
    candidate_attempt: int | None = None
    publication_state: str = 'none'
    receipt_id: str | None = None

    def transaction(self, run_id: str) -> dict:
        return {'run_id': run_id, 'attempt_no': self.candidate_attempt,
                'revision': self.candidate_revision, 'receipt_id': self.receipt_id}


class SnapshotRunStore:
    def __init__(self, store: SqliteRunStore, limits: SnapshotLimits):
        self.store, self.limits = store, limits

    def _checked(self, raw: bytes, revision: str) -> None:
        snapshot = decode_snapshot(raw, self.limits)
        if encode_snapshot(snapshot, self.limits) != raw or snapshot_revision(raw) != revision:
            raise RunStoreError('persisted snapshot digest or canonical encoding mismatch')

    async def initialize(self, run_id: str, binding: dict, initial: bytes) -> None:
        revision = snapshot_revision(initial)
        self._checked(initial, revision)
        def op(db: sqlite3.Connection) -> None:
            record = db.execute('SELECT original_base_sha FROM master_run WHERE master_run_id=?',
                                (run_id,)).fetchone()
            if record is None or record[0] != revision:
                raise RunStoreError('original snapshot is not bound to this run')
            db.execute('INSERT INTO snapshot_run VALUES (?, ?, ?, ?, NULL, NULL, NULL, ?, NULL)',
                       (run_id, json.dumps(binding, sort_keys=True), revision, initial, 'none'))
        await self.store._run(op)

    async def load(self, run_id: str) -> SnapshotCheckpoint | None:
        def op(db: sqlite3.Connection) -> SnapshotCheckpoint | None:
            maximum = 4 * self.limits.max_total_bytes + 65536 * self.limits.max_files + 8192
            lengths = db.execute('SELECT length(initial), length(candidate), length(binding) '
                                 'FROM snapshot_run WHERE master_run_id=?', (run_id,)).fetchone()
            if lengths is None:
                return None
            if (lengths[0] is None or lengths[0] > maximum or (lengths[1] or 0) > maximum
                    or lengths[2] > 16384):
                raise RunStoreError('persisted recovery payload exceeds bounds')
            row = db.execute('SELECT * FROM snapshot_run WHERE master_run_id=?',
                             (run_id,)).fetchone()
            assert row is not None
            return SnapshotCheckpoint(
                json.loads(row['binding']), bytes(row['initial']), row['original_revision'],
                bytes(row['candidate']) if row['candidate'] is not None else None,
                row['candidate_revision'], row['candidate_attempt'], row['publication_state'],
                row['receipt_id'],
            )
        state = await self.store._run(op)
        if state is not None:
            self._checked(state.initial, state.original_revision)
            if state.candidate is not None:
                if state.candidate_revision is None:
                    raise RunStoreError('missing frozen revision')
                self._checked(state.candidate, state.candidate_revision)
            if state.publication_state not in ('none', 'prepared', 'applied'):
                raise RunStoreError('invalid publication receipt state')
            if state.publication_state != 'none':
                from codeagent.execution.publication import WorkspacePublication
                WorkspacePublication._validate_transaction(state.transaction(run_id))
        return state

    async def freeze(self, run_id: str, attempt: int, raw: bytes) -> None:
        revision = snapshot_revision(raw)
        self._checked(raw, revision)
        def op(db: sqlite3.Connection) -> None:
            changed = db.execute('UPDATE snapshot_run SET candidate=?, candidate_revision=?, '
                                 'candidate_attempt=?, publication_state=?, receipt_id=NULL '
                                 'WHERE master_run_id=? AND publication_state!=?',
                                 (raw, revision, attempt, 'none', run_id, 'applied')).rowcount
            if changed != 1:
                raise RunStoreError('cannot replace a published or missing checkpoint')
        await self.store._run(op)

    async def prepare(self, run_id: str, attempt: int, revision: str) -> SnapshotCheckpoint:
        def op(db: sqlite3.Connection) -> None:
            row = db.execute('SELECT state, candidate_sha FROM attempt WHERE master_run_id=? '
                             'AND attempt_no=?', (run_id, attempt)).fetchone()
            if row is None or tuple(row) != (AttemptState.PROMOTING, revision):
                raise RunStoreError('publication requires a verified PROMOTING attempt')
            changed = db.execute('UPDATE snapshot_run SET publication_state=?, '
                                 'receipt_id=COALESCE(receipt_id, ?) WHERE master_run_id=? '
                                 'AND candidate_attempt=? AND candidate_revision=? '
                                 'AND publication_state IN (?, ?)',
                                 ('prepared', uuid4().hex, run_id, attempt, revision,
                                  'none', 'prepared')).rowcount
            if changed != 1:
                raise RunStoreError('frozen snapshot publication identity mismatch')
        await self.store._run(op)
        state = await self.load(run_id)
        assert state is not None
        return state

    async def applied(self, run_id: str, state: SnapshotCheckpoint) -> None:
        if state.candidate_attempt is None or state.receipt_id is None:
            raise RunStoreError('cannot acknowledge a missing publication identity')
        attempt_no = state.candidate_attempt
        now = datetime.now(UTC).isoformat()
        def op(db: sqlite3.Connection) -> None:
            row = db.execute('SELECT publication_state, receipt_id, candidate_revision, '
                             'candidate_attempt FROM snapshot_run WHERE master_run_id=?',
                             (run_id,)).fetchone()
            current = db.execute('SELECT status, promoted_sha FROM master_run '
                                 'WHERE master_run_id=?', (run_id,)).fetchone()
            attempt = db.execute('SELECT state, candidate_sha FROM attempt '
                                 'WHERE master_run_id=? AND attempt_no=?',
                                 (run_id, attempt_no)).fetchone()
            if (row is not None and tuple(row) == ('applied', state.receipt_id,
                    state.candidate_revision, attempt_no) and current is not None
                    and tuple(current) == ('success', state.candidate_revision)
                    and attempt is not None
                    and tuple(attempt) == (AttemptState.PROMOTED, state.candidate_revision)):
                return
            if (attempt is None or attempt['state'] not in
                    (AttemptState.PROMOTING, AttemptState.PROMOTED)
                    or attempt['candidate_sha'] != state.candidate_revision):
                raise RunStoreError('publication acknowledgement disagrees with Attempt')
            changed = db.execute('UPDATE snapshot_run SET publication_state=? '
                                 'WHERE master_run_id=? AND receipt_id=? AND candidate_revision=? '
                                 'AND candidate_attempt=? AND publication_state IN (?, ?)',
                                 ('applied', run_id, state.receipt_id, state.candidate_revision,
                                  state.candidate_attempt, 'prepared', 'applied')).rowcount
            if changed != 1:
                raise RunStoreError('cannot acknowledge an unbound publication')
            db.execute('UPDATE attempt SET state=?, updated_at=? WHERE master_run_id=? '
                       'AND attempt_no=? AND candidate_sha=?',
                       (AttemptState.PROMOTED, now, run_id, state.candidate_attempt,
                        state.candidate_revision))
            _append_transition(db, run_id, attempt_no, now)
            db.execute('UPDATE master_run SET status=?, promoted_sha=?, updated_at=? '
                       'WHERE master_run_id=?', ('success', state.candidate_revision, now, run_id))
        await self.store._run(op)
