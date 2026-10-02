"""Nonblocking controller ownership of a run, held across the entire invocation.

The trusted, local RunStore namespace owns permanent lock files. Never unlink
them on release: a waiter might have opened the old inode. No TTL/PID guessing,
and no SQLite transaction is held while models or external tools are running.
"""
from __future__ import annotations

import errno
import hashlib
import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


class RunLockError(RuntimeError):
    pass


class RunLockBusy(RunLockError):
    pass


def _attribute(module: object, name: str) -> Any:
    return getattr(module, name)


def _kernel_lock(fd: int) -> None:
    if sys.platform == 'linux':
        import fcntl
        _attribute(fcntl, 'flock')(
            fd, _attribute(fcntl, 'LOCK_EX') | _attribute(fcntl, 'LOCK_NB'),
        )
    elif sys.platform == 'win32':
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        # The documented locking region may extend beyond EOF; no write needed.
        _attribute(msvcrt, 'locking')(fd, _attribute(msvcrt, 'LK_NBLCK'), 1)
    else:
        raise RunLockError('run locking requires Linux or Windows')


class RunLeaseManager:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self._held: set[str] = set()

    @contextmanager
    def acquire(self, master_run_id: str) -> Iterator[None]:
        if not isinstance(master_run_id, str) or not master_run_id or len(master_run_id) > 256:
            raise RunLockError('invalid master run ID')
        if master_run_id in self._held:
            raise RunLockBusy(f'run {master_run_id} is already active')
        fd = -1
        if self.directory is not None:
            try:
                fd = self._open(master_run_id)
                _kernel_lock(fd)
            except BaseException as exc:
                if fd != -1:
                    os.close(fd)
                if isinstance(exc, OSError):
                    if exc.errno in (errno.EACCES, errno.EAGAIN) and fd != -1:
                        raise RunLockBusy(f'run {master_run_id} is already active') from exc
                    raise RunLockError('run lease unavailable; refusing execution') from exc
                raise
        self._held.add(master_run_id)
        try:
            yield
        finally:
            self._held.remove(master_run_id)
            if fd != -1:
                os.close(fd)

    def _open(self, master_run_id: str) -> int:
        assert self.directory is not None
        directory = self.directory.absolute()
        if directory.resolve() != directory:
            raise RunLockError('run lock directory must not follow links')
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or directory.resolve() != directory:
            raise RunLockError('invalid run lock directory')
        if sys.platform == 'linux' and (
            info.st_uid != _attribute(os, 'geteuid')() or info.st_mode & 0o077
        ):
            raise RunLockError('run lock directory must be private and caller-owned')
        name = hashlib.sha256(master_run_id.encode('utf-8')).hexdigest() + '.lock'
        path = directory / name
        if path.is_symlink() or path.resolve() != path:
            raise RunLockError('run lock must not follow links')
        flags = os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.set_inheritable(fd, False)
            opened, current = os.fstat(fd), path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
                    or path.resolve() != path):
                raise RunLockError('run lock must be a stable regular file')
            if sys.platform == 'linux' and (
                opened.st_uid != _attribute(os, 'geteuid')() or opened.st_mode & 0o077
            ):
                raise RunLockError('run lock file must be private and caller-owned')
            return fd
        except BaseException:
            os.close(fd)
            raise
