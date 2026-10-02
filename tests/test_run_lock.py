from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import subprocess
import sys
from typing import Any, cast

import pytest

from codeagent.orchestration.run_lock import RunLeaseManager, RunLockBusy, RunLockError

CHILD = '''
import sys
from pathlib import Path
from codeagent.orchestration.run_lock import RunLeaseManager
with RunLeaseManager(Path(sys.argv[1])).acquire('same'):
    print('owned', flush=True)
    sys.stdin.read()
'''


def test_separate_managers_exclude_same_run_and_allow_other_runs(tmp_path):
    a, b = RunLeaseManager(tmp_path / 'locks'), RunLeaseManager(tmp_path / 'locks')
    with a.acquire('same'):
        with pytest.raises(RunLockBusy), b.acquire('same'):
            pass
        with b.acquire('other'):
            pass
    with b.acquire('same'):
        pass
    assert len(list((tmp_path / 'locks').iterdir())) == 2


def test_lock_inode_is_retained_and_lease_is_not_inheritable(tmp_path):
    leases = RunLeaseManager(tmp_path / 'locks')
    with leases.acquire('../untrusted/id'):
        path, = (tmp_path / 'locks').iterdir()
        assert path.name == hashlib.sha256(b'../untrusted/id').hexdigest() + '.lock'
        inode = path.stat().st_ino
    with leases.acquire('../untrusted/id'):
        assert path.stat().st_ino == inode


@pytest.mark.parametrize('end', ['normal', 'kill'])
async def test_actual_child_exclusion_and_process_exit_release(tmp_path, end):
    directory = tmp_path / 'locks'
    proc = await asyncio.to_thread(
        subprocess.Popen, [sys.executable, '-c', CHILD, str(directory)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert proc.stdout is not None
        line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 10)
        assert line.strip() == 'owned'
        leases = RunLeaseManager(directory)
        with pytest.raises(RunLockBusy), leases.acquire('same'):
            pass
        with leases.acquire('other'):
            pass
        if end == 'kill':
            proc.kill()
        else:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.stdin = None
        await asyncio.to_thread(proc.wait, 10)
        with leases.acquire('same'):
            pass
    finally:
        if proc.poll() is None:
            proc.kill()
        await asyncio.to_thread(proc.communicate, timeout=10)


@pytest.mark.parametrize('kind', ['symlink', 'hardlink', 'fifo', 'permissions'])
@pytest.mark.skipif(sys.platform != 'linux', reason='POSIX path protection; independent VM only')
def test_unsafe_lock_state_is_rejected(tmp_path, kind):
    directory = tmp_path / 'locks'
    directory.mkdir(mode=0o700)
    path = directory / (hashlib.sha256(b'same').hexdigest() + '.lock')
    other = tmp_path / 'other'
    other.write_text('keep')
    other.chmod(0o600)
    if kind == 'symlink':
        path.symlink_to(other)
    elif kind == 'hardlink':
        os.link(other, path)
    elif kind == 'fifo':
        cast(Any, os).mkfifo(path)
    else:
        path.touch(mode=0o644)
        path.chmod(0o644)
    with pytest.raises(RunLockError), RunLeaseManager(directory).acquire('same'):
        pass
    assert other.read_text() == 'keep'


def test_kernel_failure_does_not_fall_back_to_unlocked_execution(tmp_path, monkeypatch):
    def broken(fd):
        assert not os.get_inheritable(fd)
        raise OSError(errno.EIO, 'failure')
    monkeypatch.setattr('codeagent.orchestration.run_lock._kernel_lock', broken)
    with pytest.raises(RunLockError, match='unavailable'):
        with RunLeaseManager(tmp_path / 'locks').acquire('same'):
            pytest.fail('must not enter')


async def test_cancelled_owner_releases_for_next_invocation(tmp_path):
    acquired = asyncio.Event()
    a, b = RunLeaseManager(tmp_path / 'locks'), RunLeaseManager(tmp_path / 'locks')
    async def owner():
        with a.acquire('same'):
            acquired.set()
            await asyncio.Event().wait()
    task = asyncio.create_task(owner())
    await acquired.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with b.acquire('same'):
        pass
