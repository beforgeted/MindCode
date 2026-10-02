"""Trusted, bounded handoff between a disposable Git worktree and a sandbox.

Publishing touches only the caller-owned, isolated Worker worktree. Partial IO
failure invalidates that Worker; the candidate/base transaction remains upstream.
No untrusted process is ever given this host directory.
"""
from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

from codeagent.execution.snapshot import (
    SnapshotError,
    SnapshotLimits,
    TreeSnapshot,
    _directory_flags,
    _open_directory,
    _posix_flag,
    _read_tree_fd,
    apply_snapshot,
    read_tree,
    validate_snapshot,
)
from codeagent.workspace.context import WorkspaceContext


def capture_workspace(
    workspace: WorkspaceContext, limits: SnapshotLimits = SnapshotLimits(),
) -> TreeSnapshot:
    if not workspace.is_isolated:
        raise SnapshotError("sandbox handoff requires an isolated worktree")
    return read_tree(workspace.root, limits, workspace_input=True)


def publish_workspace(
    workspace: WorkspaceContext, initial: TreeSnapshot, output: TreeSnapshot,
    limits: SnapshotLimits = SnapshotLimits(),
) -> None:
    if not workspace.is_isolated:
        raise SnapshotError("cannot publish sandbox output into a shared workspace")
    validate_snapshot(initial, limits)
    validate_snapshot(output, limits)
    before = {e.path: e for e in initial.entries}
    after = {e.path: e for e in output.entries}
    # Git control metadata cannot be introduced via a worker's content changes.
    for name in before.keys() | after.keys():
        if Path(name).name.casefold() in (".gitattributes", ".gitmodules"):
            if before.get(name) != after.get(name):
                raise SnapshotError("sandbox cannot modify Git control files")
    # Validate/materialize the entire output before touching the Worker tree.
    with tempfile.TemporaryDirectory(prefix="mindcode-snapshot-") as staging:
        apply_snapshot(output, Path(staging), limits)
        root_fd = _open_directory(workspace.root)
        try:
            info = os.fstat(root_fd)
            geteuid = getattr(os, "geteuid", None)
            if not callable(geteuid) or info.st_uid != geteuid():
                raise SnapshotError("worker directory must be caller-owned")
            if _read_tree_fd(root_fd, limits, workspace_input=True) != initial:
                raise SnapshotError("worker tree changed after sandbox capture")
            # Retain the fd anchor and require private ownership throughout publication.
            fchmod = getattr(os, "fchmod", None)
            if not callable(fchmod):
                raise SnapshotError("private publication requires POSIX")
            fchmod(root_fd, stat.S_IMODE(info.st_mode) & ~0o077)
            changed = {p for p in before.keys() | after.keys() if before.get(p) != after.get(p)}
            for name in sorted(changed & before.keys()):
                parent, leaf = _parent(root_fd, name)
                try:
                    os.unlink(leaf, dir_fd=parent)
                finally:
                    os.close(parent)
            # Remove only now-empty input directories (handles directory -> file).
            directories = {str(p) for n in before for p in Path(n).parents if str(p) != "."}
            for name in sorted(directories, key=lambda p: len(Path(p).parts), reverse=True):
                parent, leaf = _parent(root_fd, name)
                try:
                    try:
                        os.rmdir(leaf, dir_fd=parent)
                    except OSError:
                        pass  # Preserved files, including excluded secrets, stay in place.
                finally:
                    os.close(parent)
            for name in sorted(changed & after.keys()):
                entry = after[name]
                parent, leaf = _parent(root_fd, name, create=True)
                try:
                    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _posix_flag("O_NOFOLLOW")
                    fd = os.open(leaf, flags, 0o700 if entry.executable else 0o600, dir_fd=parent)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(entry.data)
                        fchmod(stream.fileno(), 0o700 if entry.executable else 0o600)
                finally:
                    os.close(parent)
        finally:
            os.close(root_fd)


def _parent(root_fd: int, name: str, *, create: bool = False) -> tuple[int, str]:
    parts = name.split("/")
    parent = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, mode=0o700, dir_fd=parent)
                except FileExistsError:
                    pass
            child = os.open(part, _directory_flags(), dir_fd=parent)
            os.close(parent)
            parent = child
        return parent, parts[-1]
    except BaseException:
        os.close(parent)
        raise
