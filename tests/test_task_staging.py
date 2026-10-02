"""Recorded staging ownership, dead process recovery and fail-closed path checks."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from codeagent.execution.models import SandboxError
from codeagent.execution.task_staging import TaskStaging

pytestmark = pytest.mark.skipif(sys.platform != 'linux', reason='POSIX staging identity')


def test_active_owner_and_unknown_directories_are_preserved(tmp_path):
    first = TaskStaging(tmp_path / 'staging', '/project')
    unknown = first.directory / 'data' / ('f' * 32)
    unknown.mkdir(mode=0o700)
    try:
        (first.root / 'marker').write_text('active')
        second = TaskStaging(first.directory, '/project')
        second.close()
        assert (first.root / 'marker').read_text() == 'active' and unknown.exists()
    finally:
        first.close()
    assert unknown.exists()


def test_sigkill_owner_is_reclaimed_from_exact_ledger(tmp_path):
    directory = tmp_path / 'staging'
    child = subprocess.Popen([
        sys.executable, '-c',
        'import sys,time; from pathlib import Path; '
        'from codeagent.execution.task_staging import TaskStaging; '
        's=TaskStaging(Path(sys.argv[1]),"/project"); '
        '(s.root/"marker").write_text("orphan"); print(s.root,flush=True); time.sleep(300)',
        str(directory),
    ], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None
        root = Path(child.stdout.readline().strip())
        assert (root / 'marker').read_text() == 'orphan'
        child.kill()
        assert child.wait(timeout=5) == -9
        recovered = TaskStaging(directory, '/project')
        try:
            assert not root.exists()
        finally:
            recovered.close()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


@pytest.mark.parametrize('damage', ['replacement', 'symlink', 'unbound_nonempty'])
def test_changed_identity_is_retained_and_rejected(tmp_path, damage):
    staging = TaskStaging(tmp_path / 'staging', '/project')
    root, owner = staging.root, staging.owner
    if damage == 'unbound_nonempty':
        assert staging.db is not None
        with staging.db:
            staging.db.execute('UPDATE owners SET device=NULL, inode=NULL WHERE owner=?', (owner,))
        (root / 'marker').write_text('unbound')
    else:
        root.rename(tmp_path / 'retained')
        if damage == 'symlink':
            root.symlink_to(tmp_path / 'retained')
        else:
            root.mkdir(mode=0o700)
            (root / 'marker').write_text('replacement')
    staging.abandon()
    with pytest.raises((SandboxError, OSError)):
        TaskStaging(staging.directory, '/project')
    assert root.exists()


def test_unbound_empty_intent_can_be_reclaimed(tmp_path):
    staging = TaskStaging(tmp_path / 'staging', '/project')
    root = staging.root
    assert staging.db is not None
    with staging.db:
        staging.db.execute('UPDATE owners SET device=NULL, inode=NULL')
    staging.abandon()
    recovered = TaskStaging(staging.directory, '/project')
    recovered.close()
    assert not root.exists()


def test_delete_failure_keeps_ledger_intent(tmp_path, monkeypatch):
    staging = TaskStaging(tmp_path / 'staging', '/project')
    assert staging.db is not None
    owner = staging.owner

    def fail(*args, **kwargs):
        raise OSError('cannot remove')

    setattr(fail, 'avoids_symlink_attacks', True)  # noqa: B010 (callable test attribute)

    with monkeypatch.context() as patch:
        patch.setattr('codeagent.execution.task_staging.shutil.rmtree', fail)
        with pytest.raises((SandboxError, OSError)):
            staging._remove(owner)
    assert staging.db.execute('SELECT owner FROM owners').fetchone() == (owner,)
    staging.close()


def test_ledger_namespace_is_not_reusable_for_other_project(tmp_path):
    staging = TaskStaging(tmp_path / 'staging', '/project')
    staging.close()
    with pytest.raises(SandboxError):
        TaskStaging(staging.directory, '/other-project')


def test_unsafe_directory_is_rejected(tmp_path):
    directory = tmp_path / 'staging'
    directory.mkdir(mode=0o755)
    os.chmod(directory, 0o755)
    with pytest.raises(SandboxError):
        TaskStaging(directory, '/project')
