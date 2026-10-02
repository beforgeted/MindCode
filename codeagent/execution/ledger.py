"""Durable creation intents and kernel-held controller leases (Linux only).

The private control directory and database are trusted host state. Recovery uses
only exact names/IDs recorded here; it never enumerates containers by prefix.
An owner lease is held across Podman clients via pass_fds, so a surviving create
client prevents another controller from deciding its creation intent is absent.
"""
from __future__ import annotations

import errno
import os
import re
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from codeagent.execution.models import SandboxError


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    name: str
    owner: str
    purpose: str
    image: str
    container_id: str | None


def _required_attribute(module: object, name: str) -> Any:
    return getattr(module, name)


def _lock_file(directory: Path, owner: str) -> int:
    import fcntl

    if re.fullmatch(r"[0-9a-f]{32}", owner) is None:
        raise SandboxError("invalid ledger owner")
    fd = os.open(directory / f"{owner}.lock", os.O_RDWR | os.O_CREAT
                 | int(_required_attribute(os, "O_NOFOLLOW"))
                 | int(_required_attribute(os, "O_CLOEXEC")), 0o600)
    try:
        _private_file(fd)
        _required_attribute(fcntl, "flock")(fd, int(_required_attribute(fcntl, "LOCK_EX"))
                                | int(_required_attribute(fcntl, "LOCK_NB")))
    except BaseException:
        os.close(fd)
        raise
    return fd


def _private_file(fd: int) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != _required_attribute(os, "geteuid")()
            or info.st_nlink != 1 or info.st_mode & 0o077):
        raise SandboxError("ledger files must be private, regular and caller-owned")


class SandboxResourceLedger:
    directory: Path
    owner: str
    scope: str

    def __init__(self, directory: Path, project: str, owner: str):
        self.fd = -1
        self.db: sqlite3.Connection | None = None
        if sys.platform != "linux":
            raise SandboxError("durable sandbox leases require Linux")
        directory = directory.absolute()
        if directory.resolve() != directory:
            raise SandboxError("ledger directory must not follow symlinks")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = directory.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise SandboxError("ledger directory must be private and caller-owned")
        self.directory, self.owner = directory, owner
        self.fd = _lock_file(directory, owner)
        try:
            db_path = directory / "resources.db"
            fd = os.open(db_path, os.O_RDWR | os.O_CREAT
                         | int(_required_attribute(os, "O_NOFOLLOW"))
                         | int(_required_attribute(os, "O_CLOEXEC")), 0o600)
            try:
                _private_file(fd)
            finally:
                os.close(fd)
            self.db = sqlite3.connect(db_path, timeout=5)
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS namespace (
                    key INTEGER PRIMARY KEY CHECK(key=1), project TEXT NOT NULL,
                    scope TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS owners (owner TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS resources (
                    name TEXT PRIMARY KEY, owner TEXT NOT NULL REFERENCES owners(owner),
                    purpose TEXT NOT NULL, image TEXT NOT NULL, container_id TEXT);
            """)
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO namespace VALUES (1, ?, ?)",
                                (project, uuid4().hex))
                identity = self.db.execute("SELECT project, scope FROM namespace").fetchone()
                if identity is None or identity[0] != project:
                    raise SandboxError("sandbox ledger belongs to another project")
                self.scope = str(identity[1])
                if re.fullmatch(r"[0-9a-f]{32}", self.scope) is None:
                    raise SandboxError("invalid ledger namespace")
                self.db.execute("INSERT INTO owners VALUES (?)", (owner,))
        except BaseException:
            self.close()
            raise

    def _connection(self) -> sqlite3.Connection:
        if self.db is None or self.fd < 0:
            raise SandboxError("sandbox ledger is closed")
        return self.db

    def reserve(self, name: str, purpose: str, image: str) -> None:
        if (re.fullmatch(r"mindcode-[0-9a-f]{32}", name) is None
                or re.fullmatch(r"[0-9a-f]{64}", image) is None):
            raise SandboxError("invalid resource identity")
        db = self._connection()
        with db:
            db.execute("INSERT INTO resources VALUES (?, ?, ?, ?, NULL)",
                       (name, self.owner, purpose, image))

    def bind(self, name: str, container_id: str) -> None:
        if re.fullmatch(r"[0-9a-f]{64}", container_id) is None:
            raise SandboxError("invalid container ID")
        db = self._connection()
        with db:
            changed = db.execute(
                "UPDATE resources SET container_id=? WHERE name=? AND owner=? "
                "AND container_id IS NULL", (container_id, name, self.owner),
            ).rowcount
            if changed != 1:
                raise SandboxError("missing or already bound creation intent")

    def owners(self) -> tuple[str, ...]:
        return tuple(row[0] for row in self._connection().execute("SELECT owner FROM owners"))

    def lock_orphan(self, owner: str) -> int | None:
        if owner == self.owner:
            return None
        try:
            return _lock_file(self.directory, owner)
        except OSError as exc:
            if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                return None
            raise

    def records(self, owner: str) -> tuple[ResourceRecord, ...]:
        rows = self._connection().execute(
            "SELECT name, owner, purpose, image, container_id FROM resources WHERE owner=?",
            (owner,),
        )
        records = tuple(ResourceRecord(*row) for row in rows)
        for record in records:
            if (re.fullmatch(r"mindcode-[0-9a-f]{32}", record.name) is None
                    or re.fullmatch(r"[0-9a-f]{32}", record.owner) is None
                    or re.fullmatch(r"[0-9a-f]{64}", record.image) is None
                    or record.purpose not in ("worker", "validation", "interactive_readonly")
                    or (record.container_id is not None
                        and re.fullmatch(r"[0-9a-f]{64}", record.container_id) is None)):
                raise SandboxError("invalid resource record; recovery refused")
        return records

    def forget(self, name: str, owner: str) -> None:
        db = self._connection()
        with db:
            db.execute("DELETE FROM resources WHERE name=? AND owner=?", (name, owner))

    def finish_owner(self, owner: str) -> None:
        db = self._connection()
        with db:
            if db.execute("SELECT 1 FROM resources WHERE owner=?", (owner,)).fetchone():
                raise SandboxError("cannot retire an owner with pending resources")
            db.execute("DELETE FROM owners WHERE owner=?", (owner,))
        # The random owner is never reused. A competing reclaimer must reread the
        # resource rows after acquiring its lease, so an empty retired owner has
        # no authority even if a stale reader re-creates this lock file.
        (self.directory / f"{owner}.lock").unlink(missing_ok=True)

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.fd >= 0:
            os.close(self.fd)  # Do not LOCK_UN: surviving clients share the lease.
            self.fd = -1

    def __del__(self) -> None:
        self.close()
