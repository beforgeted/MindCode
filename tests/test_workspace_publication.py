from __future__ import annotations

import subprocess
import sys

import pytest

from codeagent.execution.publication import PublicationUncertain, WorkspacePublication
from codeagent.execution.snapshot import SnapshotEntry, SnapshotError, TreeSnapshot, read_tree

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux journaled publication")


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "a").write_bytes(b"old-a")
    (root / "b").write_bytes(b"old-b")
    return root, tmp_path / "state", read_tree(root)


def changed():
    return TreeSnapshot((SnapshotEntry("a", b"new-a"), SnapshotEntry("b", b"new-b")))


def test_only_delta_published_and_permissions_preserved(tree):
    root, state, initial = tree
    (root / "a").chmod(0o640)
    mode = root.stat().st_mode
    with WorkspacePublication(root, state) as transaction:
        transaction.publish(initial, changed(), capture=lambda: read_tree(root), expected_guard="")
    assert read_tree(root) == changed()
    assert (root / "a").stat().st_mode & 0o777 == 0o640
    assert root.stat().st_mode == mode
    assert not (state / "journal.json").exists()
    assert not list(root.glob(".mindcode-write-*"))


def test_second_write_failure_rolls_back_entire_delta(tree, monkeypatch):
    root, state, initial = tree
    with WorkspacePublication(root, state) as transaction:
        write = transaction._write

        def fail(name, value, mode):
            if value is not None and value.data == b"new-b":
                raise OSError("disk full")
            write(name, value, mode)

        monkeypatch.setattr(transaction, "_write", fail)
        with pytest.raises(OSError, match="disk full"):
            transaction.publish(
                initial, changed(), capture=lambda: read_tree(root), expected_guard="",
            )
    assert read_tree(root) == initial
    assert not (state / "journal.json").exists()


def test_rollback_conflict_preserves_editor_and_journal(tree, monkeypatch):
    root, state, initial = tree
    with WorkspacePublication(root, state) as transaction:
        write = transaction._write

        def fail(name, value, mode):
            if name == "b":
                (root / "a").write_bytes(b"editor")
                raise OSError("failure")
            write(name, value, mode)

        monkeypatch.setattr(transaction, "_write", fail)
        with pytest.raises(PublicationUncertain, match="待核对"):
            transaction.publish(
                initial, changed(), capture=lambda: read_tree(root), expected_guard="",
            )
    assert (root / "a").read_bytes() == b"editor"
    assert (state / "journal.json").exists()
    with pytest.raises(PublicationUncertain, match="用户"):
        WorkspacePublication(root, state)
    assert (root / "a").read_bytes() == b"editor"


@pytest.mark.parametrize("window", ["prepared", "temporary", "partial", "applied"])
def test_sigkill_journal_recovery(tree, window):
    root, state, initial = tree
    source = '''
import os, signal, sys
from pathlib import Path
from codeagent.execution.publication import WorkspacePublication
from codeagent.execution.snapshot import SnapshotEntry, TreeSnapshot, read_tree
from codeagent.execution.workspace import _parent
root, state = Path(sys.argv[1]), Path(sys.argv[2])
window = sys.argv[3]
def kill():
    os.kill(os.getpid(), signal.SIGKILL)
class Crash(WorkspacePublication):
    def _save(self, record):
        super()._save(record)
        if record['state'] == window:
            kill()
    def _write(self, name, value, mode):
        if window == 'temporary':
            parent, leaf = _parent(self.root_fd, name)
            fd = os.open(self._temporary(name), os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                         0o600, dir_fd=parent)
            os.write(fd, b'pending')
            os.fsync(fd)
            kill()
        super()._write(name, value, mode)
        if window == 'partial':
            kill()
initial = read_tree(root)
output = TreeSnapshot((SnapshotEntry('a', b'new-a'), SnapshotEntry('b', b'new-b')))
with Crash(root, state) as transaction:
    transaction.publish(initial, output, capture=lambda: read_tree(root), expected_guard='')
'''
    process = subprocess.run(
        [sys.executable, "-I", "-c", source, str(root), str(state), window],
        capture_output=True, timeout=15,
    )
    assert process.returncode == -9, process.stderr.decode()
    assert (state / "journal.json").exists()
    with WorkspacePublication(root, state):
        pass
    assert read_tree(root) == (changed() if window == "applied" else initial)
    assert not list(root.glob(".mindcode-write-*"))
    assert not (state / "journal.json").exists()


def test_new_directories_and_deletion_roll_back(tree, monkeypatch):
    root, state, initial = tree
    output = TreeSnapshot((SnapshotEntry("new/nested/x", b"X"),))
    with WorkspacePublication(root, state) as transaction:
        save = transaction._save

        def fail_commit(record):
            if record["state"] == "applied":
                raise OSError("commit marker unavailable")
            save(record)

        monkeypatch.setattr(transaction, "_save", fail_commit)
        with pytest.raises(OSError):
            transaction.publish(initial, output, capture=lambda: read_tree(root), expected_guard="")
    assert read_tree(root) == initial
    assert not (root / "new").exists()


@pytest.mark.parametrize("change", ["source", "guard"])
def test_source_or_guard_change_prevents_all_writes(tree, change):
    root, state, initial = tree
    marker = {"value": "old"}
    with WorkspacePublication(root, state, guard=lambda: marker["value"]) as transaction:
        if change == "source":
            (root / "a").write_bytes(b"editor")
        else:
            marker["value"] = "new"
        with pytest.raises(SnapshotError, match="变化"):
            transaction.publish(initial, changed(), capture=lambda: read_tree(root),
                                expected_guard="old")
    assert (root / "b").read_bytes() == b"old-b"
    assert not (state / "journal.json").exists()


def test_live_writer_lease_refuses_second_instance(tree):
    root, state, _ = tree
    with WorkspacePublication(root, state):
        with pytest.raises(BlockingIOError):
            WorkspacePublication(root, state)


def test_shared_publication_refuses_file_directory_conversion(tree):
    root, state, initial = tree
    output = TreeSnapshot((SnapshotEntry("a/child", b"X"),))
    with WorkspacePublication(root, state) as transaction:
        with pytest.raises(SnapshotError, match="convert"):
            transaction.publish(initial, output, capture=lambda: read_tree(root), expected_guard="")
    assert read_tree(root) == initial


def test_corrupt_journal_preserves_uncertain_state(tree):
    root, state, initial = tree
    state.mkdir(mode=0o700)
    record = state / "journal.json"
    record.write_bytes(b'{"state":')
    record.chmod(0o600)
    with pytest.raises(PublicationUncertain, match="待核对"):
        WorkspacePublication(root, state)
    assert record.read_bytes() == b'{"state":'
    assert read_tree(root) == initial


def test_editor_change_to_other_file_during_write_is_preserved(tree, monkeypatch):
    root, state, initial = tree
    with WorkspacePublication(root, state) as transaction:
        write = transaction._write

        def edit(name, value, mode):
            write(name, value, mode)
            if name == "b" and value is not None and value.data == b"new-b":
                (root / "editor").write_bytes(b"new user file")

        monkeypatch.setattr(transaction, "_write", edit)
        with pytest.raises(SnapshotError, match="结束"):
            transaction.publish(
                initial, changed(), capture=lambda: read_tree(root), expected_guard="",
            )
    assert (root / "a").read_bytes() == b"old-a"
    assert (root / "b").read_bytes() == b"old-b"
    assert (root / "editor").read_bytes() == b"new user file"
    assert not (state / "journal.json").exists()


def test_applied_marker_confirmation_failure_is_reported_uncertain(tree, monkeypatch):
    root, state, initial = tree
    with WorkspacePublication(root, state) as transaction:
        save = transaction._save

        def fail_confirmation(record):
            save(record)
            if record["state"] == "applied":
                raise OSError("directory sync failed after marker replace")

        monkeypatch.setattr(transaction, "_save", fail_confirmation)
        with pytest.raises(PublicationUncertain, match="待核对"):
            transaction.publish(
                initial, changed(), capture=lambda: read_tree(root), expected_guard="",
            )
    assert read_tree(root) == changed()
    assert (state / "journal.json").exists()
    with WorkspacePublication(root, state):
        pass
    assert read_tree(root) == changed()
    assert not (state / "journal.json").exists()
