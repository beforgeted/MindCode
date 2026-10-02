"""Linux private staging: durable creation intents and kernel-held owner leases.

This ledger owns disposable disk resources, never task recovery decisions.
Only exact recorded directories are reclaimed, after proving the owner inactive.
"""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
from contextlib import AbstractContextManager
from pathlib import Path
from uuid import uuid4

from codeagent.execution.ledger import _private_file, _required_attribute
from codeagent.execution.models import SandboxError
from codeagent.execution.snapshot import _open_directory, _posix_flag
from codeagent.orchestration.run_lock import RunLeaseManager, RunLockBusy


class TaskStaging:
    def __init__(self, directory: Path, project: str):
        self.directory = directory.absolute()
        self.db: sqlite3.Connection | None = None
        self._lease: AbstractContextManager | None = None
        self.owner = uuid4().hex
        self.root = self.directory / 'data' / self.owner
        if self.directory.resolve() != self.directory:
            raise SandboxError('staging directory must not follow links')
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        self._private_directory(self.directory)
        self.leases = RunLeaseManager(self.directory / 'leases')
        try:
            path = self.directory / 'resources.db'
            fd = os.open(path, os.O_RDWR | os.O_CREAT | _posix_flag('O_NOFOLLOW'), 0o600)
            try:
                _private_file(fd)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(path, timeout=5)
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.executescript('''
                CREATE TABLE IF NOT EXISTS namespace (key INTEGER PRIMARY KEY, project TEXT);
                CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY, device INTEGER,
                                                  inode INTEGER);
            ''')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO namespace VALUES(1, ?)', (project,))
                bound = self.db.execute('SELECT project FROM namespace WHERE key=1').fetchone()
                if bound != (project,):
                    raise SandboxError('staging ledger belongs to another project')
            data = self.directory / 'data'
            data.mkdir(mode=0o700, exist_ok=True)
            self._private_directory(data)
            self.reclaim()
            lease = self.leases.acquire(self.owner)
            lease.__enter__()
            self._lease = lease
            # Persist the exact creation intent before creating any disk resource.
            with self.db:
                self.db.execute('INSERT INTO owners VALUES (?, NULL, NULL)', (self.owner,))
            self.root.mkdir(mode=0o700)
            info = self.root.stat()
            self._sync(data)
            with self.db:
                self.db.execute('UPDATE owners SET device=?, inode=? WHERE owner=?',
                                (info.st_dev, info.st_ino, self.owner))
        except BaseException:
            self.abandon()
            raise

    @staticmethod
    def _sync(path: Path) -> None:
        fd = _open_directory(path)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _private_directory(path: Path) -> None:
        fd = _open_directory(path)
        try:
            info = os.fstat(fd)
            if info.st_uid != _required_attribute(os, 'geteuid')() or info.st_mode & 0o077:
                raise SandboxError('staging directories must be private and caller-owned')
        finally:
            os.close(fd)

    def _remove(self, owner: str) -> None:
        assert self.db is not None
        if re.fullmatch('[0-9a-f]{32}', owner) is None:
            raise SandboxError('invalid staging owner')
        row = self.db.execute('SELECT device, inode FROM owners WHERE owner=?', (owner,)).fetchone()
        if row is None:
            return  # A concurrent reclaimer may already have retired the intent.
        path = self.directory / 'data' / owner
        try:
            info = path.lstat()
        except FileNotFoundError:
            info = None
        if info is not None:
            self._private_directory(path)
            if path.resolve() != path or not stat.S_ISDIR(info.st_mode):
                raise SandboxError('staging identity changed; preserve recorded resource')
            if row == (None, None):
                path.rmdir()  # An unbound creation can only be an empty directory.
            elif row == (info.st_dev, info.st_ino):
                if not shutil.rmtree.avoids_symlink_attacks:
                    raise SandboxError('staging cleanup requires anchored POSIX rmtree')
                shutil.rmtree(path)
            else:
                raise SandboxError('staging inode changed; preserve recorded resource')
            self._sync(path.parent)
        with self.db:
            self.db.execute('DELETE FROM owners WHERE owner=?', (owner,))

    def reclaim(self) -> None:
        assert self.db is not None
        # Never enumerate filesystem directories to infer ownership.
        owners = [row[0] for row in self.db.execute('SELECT owner FROM owners')]
        for owner in owners:
            if owner == self.owner:
                continue
            try:
                with self.leases.acquire(owner):
                    self._remove(owner)
            except RunLockBusy:
                continue

    def close(self) -> None:
        try:
            if self.db is not None:
                self._remove(self.owner)
        finally:
            self.abandon()

    def abandon(self) -> None:
        """Close handles without forgetting intents; crashes follow the same recovery path."""
        if self.db is not None:
            self.db.close()
            self.db = None
        if self._lease is not None:
            self._lease.__exit__(None, None, None)
            self._lease = None
