"""Journaled, file-level publication into a caller-owned shared workspace.

An advisory lease serializes MindCode writers. Snapshot comparisons detect
observed editor changes, but do not constitute a filesystem-wide atomic CAS
against editors which ignore the lease. Interrupted writes roll back on reopen;
conflicting external changes preserve the journal and require inspection.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from codeagent.execution.ledger import _private_file, _required_attribute
from codeagent.execution.models import SandboxError
from codeagent.execution.snapshot import (
    SnapshotEntry,
    SnapshotError,
    SnapshotLimits,
    TreeSnapshot,
    _open_directory,
    _posix_flag,
    _read_tree_fd,
    decode_snapshot,
    encode_snapshot,
    validate_snapshot,
)
from codeagent.execution.workspace import _parent


class PublicationUncertain(SandboxError):
    """A partial handoff cannot safely be confirmed or rolled back automatically."""


class WorkspacePublication:
    def __init__(
        self, root: Path, directory: Path, *, limits: SnapshotLimits = SnapshotLimits(),
        guard: Callable[[], str] = lambda: "",
        task_run_id: str | None = None,
    ):
        self.root, self.directory, self.limits, self.guard = root, directory, limits, guard
        self.root_fd = _open_directory(root)
        self.directory_fd = -1
        self.lock_fd = -1
        self.nonce = uuid4().hex
        self.task_run_id = task_run_id
        try:
            uid = _required_attribute(os, "geteuid")()
            if os.fstat(self.root_fd).st_uid != uid:
                raise SnapshotError("shared workspace must be caller-owned")
            directory.mkdir(parents=True, mode=0o700, exist_ok=True)
            self.directory_fd = _open_directory(directory)
            info = os.fstat(self.directory_fd)
            if info.st_uid != uid or info.st_mode & 0o077:
                raise SnapshotError("publication control directory must be private")
            self.lock_fd = os.open(
                "writer.lock", os.O_RDWR | os.O_CREAT | _posix_flag("O_NOFOLLOW")
                | _posix_flag("O_CLOEXEC"), 0o600, dir_fd=self.directory_fd,
            )
            _private_file(self.lock_fd)
            import fcntl
            _required_attribute(fcntl, "flock")(
                self.lock_fd, int(_required_attribute(fcntl, "LOCK_EX"))
                | int(_required_attribute(fcntl, "LOCK_NB")),
            )
            try:
                self.recover()
            except PublicationUncertain:
                raise
            except Exception as exc:
                raise PublicationUncertain("写回记录无法安全恢复，已保留现场待核对") from exc
        except BaseException:
            self.close()
            raise

    def _identity(self) -> tuple[int, int]:
        info = os.fstat(self.root_fd)
        current = _open_directory(self.root)
        try:
            other = os.fstat(current)
            if (info.st_dev, info.st_ino) != (other.st_dev, other.st_ino):
                raise PublicationUncertain("工作区目录身份已变化，保留写回记录")
        finally:
            os.close(current)
        return info.st_dev, info.st_ino

    def _save(self, record: dict) -> None:
        payload = json.dumps(record).encode()
        fd = os.open("journal.tmp", os.O_WRONLY | os.O_CREAT | os.O_EXCL
                     | _posix_flag("O_NOFOLLOW"), 0o600, dir_fd=self.directory_fd)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace("journal.tmp", "journal.json", src_dir_fd=self.directory_fd,
                       dst_dir_fd=self.directory_fd)
            os.fsync(self.directory_fd)
        finally:
            try:
                os.unlink("journal.tmp", dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass

    def _retire(self) -> None:
        os.unlink("journal.json", dir_fd=self.directory_fd)
        os.fsync(self.directory_fd)

    def _current(self, name: str) -> SnapshotEntry | None:
        entries = _read_tree_fd(self.root_fd, self.limits, include_paths=frozenset((name,))).entries
        return next((entry for entry in entries if entry.path == name), None)

    def _write(self, name: str, value: SnapshotEntry | None, mode: int) -> None:
        if value is None:
            try:
                parent, leaf = _parent(self.root_fd, name)
            except FileNotFoundError:
                return
            try:
                try:
                    os.unlink(leaf, dir_fd=parent)
                    os.fsync(parent)
                except FileNotFoundError:
                    pass
            finally:
                os.close(parent)
            return
        parent, leaf = _parent(self.root_fd, name, create=True)
        temporary = self._temporary(name)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | _posix_flag("O_NOFOLLOW"), 0o600, dir_fd=parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(value.data)
                fchmod = _required_attribute(os, "fchmod")
                fchmod(stream.fileno(), mode)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, leaf, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary, dir_fd=parent)
            except FileNotFoundError:
                pass
            os.close(parent)

    def publish(
        self, initial: TreeSnapshot, output: TreeSnapshot, *,
        capture: Callable[[], TreeSnapshot], expected_guard: str,
        transaction: dict | None = None,
    ) -> None:
        if self._read_record() is not None:
            raise PublicationUncertain("已有待确认的任务发布回执；请先恢复对应run")
        if transaction is not None:
            self._validate_transaction(transaction)
            if transaction['run_id'] != self.task_run_id:
                raise PublicationUncertain('publication belongs to another run')
        validate_snapshot(initial, self.limits)
        validate_snapshot(output, self.limits)
        before = {entry.path: entry for entry in initial.entries}
        after = {entry.path: entry for entry in output.entries}
        names = before.keys() | after.keys()
        for name in names:
            if Path(name).name.casefold() in (".gitattributes", ".gitmodules"):
                if before.get(name) != after.get(name):
                    raise SnapshotError("sandbox cannot modify Git control files")
            if any(str(parent) in names for parent in Path(name).parents if str(parent) != "."):
                raise SnapshotError("shared publication cannot convert files and directories")
        changed = sorted(name for name in names if before.get(name) != after.get(name))
        self._identity()
        if self.guard() != expected_guard or capture() != initial:
            raise SnapshotError("工作区或暂存区在交互期间变化，拒绝写回")
        modes = {}
        directories = set()
        for name in changed:
            if name in before:
                parent, leaf = _parent(self.root_fd, name)
                try:
                    modes[name] = stat.S_IMODE(os.stat(
                        leaf, dir_fd=parent, follow_symlinks=False,
                    ).st_mode) & 0o777
                finally:
                    os.close(parent)
            for ancestor in Path(name).parents:
                if str(ancestor) == ".":
                    continue
                try:
                    parent, leaf = _parent(self.root_fd, ancestor.as_posix())
                    try:
                        os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                    finally:
                        os.close(parent)
                except FileNotFoundError:
                    directories.add(ancestor.as_posix())
        record = {
            "version": 1, "root": str(self.root.absolute()), "identity": self._identity(),
            "state": "prepared", "guard": expected_guard, "modes": modes,
            "nonce": self.nonce,
            "directories": sorted(directories),
            "initial": json.loads(encode_snapshot(initial, self.limits)),
            "output": json.loads(encode_snapshot(output, self.limits)),
        }
        if transaction is not None:
            record['transaction'] = transaction
        self._save(record)
        committing = False
        try:
            for name in changed:
                self._identity()
                if self.guard() != expected_guard or self._current(name) != before.get(name):
                    raise SnapshotError("写回期间观察到并发修改")
                mode = modes.get(name, 0o644) & ~0o111
                value = after.get(name)
                if value is not None and value.executable:
                    mode |= 0o111
                self._write(name, value, mode)
            for name in changed:
                if self._current(name) != after.get(name):
                    raise SnapshotError("写回后文件状态不一致")
            self._identity()
            if self.guard() != expected_guard or capture() != output:
                raise SnapshotError("写回结束时工作区或暂存区变化")
            record["state"] = "applied"
            committing = True
            self._save(record)
        except Exception:
            try:
                if committing:
                    decision = self._read_record()
                    if decision is None or decision.get("state") != "prepared":
                        raise PublicationUncertain("发布决定持久化确认失败，结果待核对")
                self.recover()
            except Exception as exc:
                raise PublicationUncertain("写回结果待核对；回滚冲突，已保留恢复记录") from exc
            raise
        if transaction is None:
            try:
                self._retire()
            except OSError:
                pass  # The durable applied decision is authoritative; retire on reopen.

    @staticmethod
    def _validate_transaction(value: object) -> None:
        if (not isinstance(value, dict)
                or set(value) != {'run_id', 'attempt_no', 'revision', 'receipt_id'}
                or not isinstance(value['run_id'], str) or not 1 <= len(value['run_id']) <= 256
                or type(value['attempt_no']) is not int or value['attempt_no'] < 1
                or not isinstance(value['revision'], str)
                or re.fullmatch('snapshot:[0-9a-f]{64}', value['revision']) is None
                or not isinstance(value['receipt_id'], str)
                or re.fullmatch('[0-9a-f]{32}', value['receipt_id']) is None):
            raise PublicationUncertain('invalid publication transaction identity')

    def applied_transaction(self, transaction: dict, initial: TreeSnapshot,
                            output: TreeSnapshot) -> bool:
        record = self._read_record()
        if record is None:
            return False
        self._validate_transaction(record.get('transaction'))
        if (record['state'] != 'applied' or record['transaction'] != transaction
                or decode_snapshot(json.dumps(record['initial']).encode(), self.limits) != initial
                or decode_snapshot(json.dumps(record['output']).encode(), self.limits) != output):
            raise PublicationUncertain('journal and RunStore publication receipt disagree')
        return True

    def acknowledge(self, transaction: dict) -> None:
        record = self._read_record()
        if record is None:
            return
        if record.get('state') != 'applied' or record.get('transaction') != transaction:
            raise PublicationUncertain('cannot retire another publication receipt')
        self._retire()

    def _read_record(self) -> dict | None:
        try:
            fd = os.open("journal.json", os.O_RDONLY | _posix_flag("O_NOFOLLOW"),
                         dir_fd=self.directory_fd)
        except FileNotFoundError:
            # Only an uncommitted metadata temporary is safe to discard.
            try:
                os.unlink("journal.tmp", dir_fd=self.directory_fd)
            except FileNotFoundError:
                pass
            return None
        with os.fdopen(fd, "rb") as stream:
            _private_file(stream.fileno())
            maximum = 4 * self.limits.max_total_bytes + 65536 * self.limits.max_files + 8192
            raw = stream.read(maximum + 1)
            if len(raw) > maximum:
                raise PublicationUncertain("写回记录超限，拒绝恢复")
            record = json.loads(raw)
        if not isinstance(record, dict):
            raise PublicationUncertain("写回记录格式无效")
        return record

    def recover(self) -> None:
        record = self._read_record()
        if record is None:
            return
        if (record.get("version") != 1 or record.get("root") != str(self.root.absolute())
                or record.get("identity") != list(self._identity())
                or record.get("state") not in ("prepared", "applied")):
            raise PublicationUncertain("写回记录归属或状态不符")
        nonce = record.get("nonce")
        if (not isinstance(nonce, str) or len(nonce) != 32
                or any(c not in "0123456789abcdef" for c in nonce)):
            raise PublicationUncertain("写回记录 nonce 无效")
        self.nonce = nonce
        transaction = record.get('transaction')
        if transaction is not None:
            self._validate_transaction(transaction)
        if record["state"] == "applied":
            if transaction is not None:
                if transaction['run_id'] != self.task_run_id:
                    raise PublicationUncertain("任务写回已完成但回执待记账，请先resume对应run")
                return  # Keep the handoff evidence until the RunStore acknowledges it.
            self._retire()
            return
        initial = decode_snapshot(json.dumps(record["initial"]).encode(), self.limits)
        output = decode_snapshot(json.dumps(record["output"]).encode(), self.limits)
        before = {entry.path: entry for entry in initial.entries}
        after = {entry.path: entry for entry in output.entries}
        changed = sorted(name for name in before.keys() | after.keys()
                         if before.get(name) != after.get(name))
        if (not isinstance(record["modes"], dict)
                or any(name not in before or type(mode) is not int or not 0 <= mode <= 0o777
                       for name, mode in record["modes"].items())):
            raise PublicationUncertain("写回记录权限无效")
        for name in record["directories"]:
            validate_snapshot(TreeSnapshot((SnapshotEntry(name + "/sentinel", b""),)), self.limits)
        values = {name: self._current(name) for name in changed}
        for name, value in values.items():
            if value not in (before.get(name), after.get(name)):
                raise PublicationUncertain("恢复时发现用户的新修改，保留写回记录")
        if any(values[name] != before.get(name) for name in changed):
            if self.guard() != record["guard"]:
                raise PublicationUncertain("恢复时项目版本或暂存区变化，拒绝自动回滚")
        for name in changed:
            try:
                parent, _ = _parent(self.root_fd, name)
            except FileNotFoundError:
                continue
            try:
                temporary = self._temporary(name)
                try:
                    info = os.stat(temporary, dir_fd=parent, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != _required_attribute(os, "geteuid")()):
                        raise PublicationUncertain("写回临时文件身份不符")
                    os.unlink(temporary, dir_fd=parent)
                    os.fsync(parent)
                except FileNotFoundError:
                    pass
            finally:
                os.close(parent)
        for name in reversed(changed):
            if values[name] == before.get(name):
                continue
            if self._current(name) != values[name] or self.guard() != record["guard"]:
                raise PublicationUncertain("回滚期间发现并发修改，已保留记录")
            mode = record["modes"].get(name, 0o644)
            self._write(name, before.get(name), mode)
        for name in sorted(record["directories"], key=lambda n: len(Path(n).parts), reverse=True):
            try:
                parent, leaf = _parent(self.root_fd, name)
            except FileNotFoundError:
                continue
            try:
                try:
                    os.rmdir(leaf, dir_fd=parent)
                    os.fsync(parent)
                except OSError:
                    pass  # Never remove a directory populated by another writer.
            finally:
                os.close(parent)
        self._retire()

    def _temporary(self, name: str) -> str:
        digest = hashlib.sha256(name.encode()).hexdigest()[:24]
        return ".mindcode-write-" + self.nonce + "-" + digest

    def close(self) -> None:
        for name in ("lock_fd", "directory_fd", "root_fd"):
            fd = getattr(self, name, -1)
            if fd >= 0:
                os.close(fd)
                setattr(self, name, -1)

    def __enter__(self) -> WorkspacePublication:
        return self

    def __exit__(self, *args) -> None:
        self.close()
