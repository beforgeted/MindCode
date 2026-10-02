"""Current shared-tree input, excluding credentials, state and ignored artifacts."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from codeagent.execution.snapshot import SnapshotError, SnapshotLimits, TreeSnapshot, read_tree


def git_data(
    root: Path, *args: str, data: bytes | None = None, allow_absent: bool = False,
) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args], input=data,
        capture_output=True, timeout=30,
    )
    if result.returncode and not (allow_absent and result.returncode == 1):
        raise SnapshotError("trusted Git input query failed")
    if len(result.stdout) > 4 * 1024 * 1024:
        raise SnapshotError("Git input metadata exceeds limit")
    return result.stdout


def git_guard(root: Path) -> str:
    head = git_data(root, "rev-parse", "HEAD")
    branch = git_data(root, "symbolic-ref", "-q", "HEAD", allow_absent=True)
    index = git_data(root, "ls-files", "--stage", "-z")
    return hashlib.sha256(head + b"\0" + branch + b"\0" + index).hexdigest()


def _protected(name: str) -> bool:
    return any(part.casefold() in (".git", ".codeagent", ".env")
               or part.casefold().startswith(".env.") for part in name.split("/"))


def capture_shared(
    root: Path, limits: SnapshotLimits, *, git: bool, excluded: frozenset[str],
) -> TreeSnapshot:
    paths = None
    if git:
        raw = git_data(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
        names = raw.decode("utf-8", errors="strict").split("\0")
        if len(names) > limits.max_files + 1:
            raise SnapshotError("Git input file count exceeds limit")
        paths = frozenset(name for name in names if name and not _protected(name))
    return read_tree(
        root, limits, workspace_input=True, include_paths=paths, exclude_paths=excluded,
    )


def accepted_output(
    root: Path, output: TreeSnapshot, *, git: bool, excluded: frozenset[str],
) -> TreeSnapshot:
    names = [entry.path for entry in output.entries]
    ignored: set[str] = set()
    if git and names:
        tracked = set(git_data(root, "ls-files", "--cached", "-z").decode("utf-8").split("\0"))
        payload = ("\0".join(names) + "\0").encode()
        ignored = set(git_data(
            root, "check-ignore", "--no-index", "-z", "--stdin", data=payload, allow_absent=True,
        ).decode("utf-8").split("\0")) - tracked
    return TreeSnapshot(tuple(entry for entry in output.entries if entry.path not in ignored
                              and not any(entry.path == p or entry.path.startswith(p + "/")
                                          for p in excluded)))
