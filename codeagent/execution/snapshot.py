"""Bounded, data-only workspace snapshots (no links or repository control files).

Filesystem operations require POSIX dirfd/O_NOFOLLOW support. Applying a snapshot
requires an existing, empty, caller-owned directory with private (0700) permissions.
The caller must keep that staging directory private for the entire operation.
"""
from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast


class SnapshotError(ValueError):
    """An unsafe, malformed, unsupported, or oversized snapshot/tree."""


@dataclass(frozen=True)
class SnapshotLimits:
    max_files: int = 4096
    max_file_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    data: bytes
    executable: bool = False


@dataclass(frozen=True)
class TreeSnapshot:
    entries: tuple[SnapshotEntry, ...]


# Also bound metadata, depth and JSON overhead independently of file contents.
_MAX_PATH_BYTES = 4096
_MAX_DEPTH = 128


def _check_limits(limits: SnapshotLimits) -> None:
    if not isinstance(limits, SnapshotLimits):
        raise SnapshotError("invalid snapshot limits")
    for value in (limits.max_files, limits.max_file_bytes, limits.max_total_bytes):
        if type(value) is not int or value < 0:
            raise SnapshotError("limits must be nonnegative integers")


def _parts(path: str) -> tuple[str, ...]:
    if type(path) is not str or not path or "\\" in path or "\x00" in path:
        raise SnapshotError("invalid snapshot path")
    if len(path) > _MAX_PATH_BYTES:
        raise SnapshotError("path too long")
    try:
        size = len(path.encode("utf-8"))
    except UnicodeError as exc:
        raise SnapshotError("invalid path Unicode") from exc
    if size > _MAX_PATH_BYTES:
        raise SnapshotError("path too long")
    parts = tuple(path.split("/"))
    if len(parts) > _MAX_DEPTH:
        raise SnapshotError("path too deep")
    for part in parts:
        # Colon also disallows Windows drives/ADS when transporting snapshots.
        if part in ("", ".", "..") or ":" in part:
            raise SnapshotError("path must be canonical relative POSIX")
        folded = part.casefold()
        if folded in (".git", ".codeagent", ".env") or folded.startswith(".env."):
            raise SnapshotError("protected path")
    return parts


def validate_snapshot(snapshot: TreeSnapshot, limits: SnapshotLimits = SnapshotLimits()) -> None:
    """Validate the entire snapshot, including implicit directory count/collisions."""
    _check_limits(limits)
    if not isinstance(snapshot, TreeSnapshot) or type(snapshot.entries) is not tuple:
        raise SnapshotError("invalid snapshot")
    if len(snapshot.entries) > limits.max_files:
        raise SnapshotError("entry count exceeds limit")
    nodes: dict[str, tuple[str, bool]] = {}
    total = 0
    for entry in snapshot.entries:
        if not isinstance(entry, SnapshotEntry):
            raise SnapshotError("invalid entry")
        parts = _parts(entry.path)
        if type(entry.data) is not bytes or type(entry.executable) is not bool:
            raise SnapshotError("invalid entry data or executable flag")
        if len(entry.data) > limits.max_file_bytes:
            raise SnapshotError("file size exceeds limit")
        total += len(entry.data)
        if total > limits.max_total_bytes:
            raise SnapshotError("total size exceeds limit")
        for index in range(1, len(parts) + 1):
            name = "/".join(parts[:index])
            key = name.casefold()
            is_file = index == len(parts)
            previous = nodes.get(key)
            if previous is not None:
                if previous[0] != name or previous[1] or is_file:
                    raise SnapshotError("duplicate, case collision, or file/directory conflict")
            else:
                nodes[key] = (name, is_file)
                if len(nodes) > limits.max_files:
                    raise SnapshotError("file and directory count exceeds limit")


def encode_snapshot(snapshot: TreeSnapshot, limits: SnapshotLimits = SnapshotLimits()) -> bytes:
    validate_snapshot(snapshot, limits)
    obj = {
        "version": 1,
        "entries": [
            {"path": e.path, "data": base64.b64encode(e.data).decode("ascii"),
             "executable": e.executable}
            for e in snapshot.entries
        ],
    }
    return json.dumps(obj, ensure_ascii=True, separators=(",", ":")).encode("ascii")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj: dict[str, object] = {}
    for key, value in pairs:
        if key in obj:
            raise SnapshotError("duplicate JSON key")
        obj[key] = value
    return obj


def decode_snapshot(payload: bytes, limits: SnapshotLimits = SnapshotLimits()) -> TreeSnapshot:
    _check_limits(limits)
    # ensure_ascii encoding needs at most six bytes per UTF-8 path byte.
    maximum = 128 + limits.max_files * (6 * _MAX_PATH_BYTES + 128)
    maximum += 4 * ((limits.max_total_bytes + 2 * limits.max_files) // 3)
    if type(payload) is not bytes or len(payload) > maximum:
        raise SnapshotError("invalid or oversized snapshot payload")
    try:
        obj = json.loads(payload, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise SnapshotError("invalid snapshot JSON") from exc
    if type(obj) is not dict or set(obj) != {"version", "entries"}:
        raise SnapshotError("invalid snapshot object")
    if type(obj["version"]) is not int or obj["version"] != 1:
        raise SnapshotError("unsupported snapshot version")
    raw_entries = obj["entries"]
    if type(raw_entries) is not list or len(raw_entries) > limits.max_files:
        raise SnapshotError("invalid or oversized entries")
    entries = []
    total = 0
    for raw in raw_entries:
        if type(raw) is not dict or set(raw) != {"path", "data", "executable"}:
            raise SnapshotError("invalid entry object")
        _parts(raw["path"])
        encoded = raw["data"]
        if type(encoded) is not str or type(raw["executable"]) is not bool:
            raise SnapshotError("invalid entry types")
        remaining = min(limits.max_file_bytes, limits.max_total_bytes - total)
        if len(encoded) > 4 * ((remaining + 2) // 3):
            raise SnapshotError("encoded file exceeds limit")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SnapshotError("invalid base64 data") from exc
        if len(data) > remaining or base64.b64encode(data).decode("ascii") != encoded:
            raise SnapshotError("oversized or noncanonical base64 data")
        total += len(data)
        entries.append(SnapshotEntry(raw["path"], data, raw["executable"]))
    snapshot = TreeSnapshot(tuple(entries))
    validate_snapshot(snapshot, limits)
    return snapshot


def _require_posix() -> None:
    required = ("O_NOFOLLOW", "O_DIRECTORY", "O_CLOEXEC", "O_NONBLOCK", "geteuid", "fchmod")
    if (os.name != "posix" or not all(hasattr(os, name) for name in required)
            or os.open not in os.supports_dir_fd or os.stat not in os.supports_dir_fd
            or os.mkdir not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        raise SnapshotError("secure snapshot traversal is unsupported on this platform")


def _posix_flag(name: str) -> int:
    """Resolve only after the platform guard; never substitute unsafe zero flags."""
    return int(getattr(os, name))


def _directory_flags() -> int:
    return os.O_RDONLY | _posix_flag("O_DIRECTORY") | _posix_flag("O_NOFOLLOW") | _posix_flag(
        "O_CLOEXEC")


def _open_directory(path: Path) -> int:
    """Open every component without following links, including root ancestors."""
    _require_posix()
    path = Path(path)
    if ".." in path.parts:
        raise SnapshotError("parent traversal in filesystem root")
    absolute = path.absolute()
    # This is a trusted host-selected anchor, possibly under .codeagent. Protected
    # names are forbidden in snapshot entries below it, not in its host ancestors.
    fd = os.open("/", _directory_flags())
    try:
        for part in absolute.parts[1:]:
            next_fd = os.open(part, _directory_flags(), dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _same_file(before: os.stat_result, after: os.stat_result) -> bool:
    return (before.st_dev, before.st_ino, before.st_mode, before.st_nlink,
            before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (
                after.st_dev, after.st_ino, after.st_mode, after.st_nlink,
                after.st_size, after.st_mtime_ns, after.st_ctime_ns)


def _read_tree_fd(
    root_fd: int, limits: SnapshotLimits, *, workspace_input: bool = False,
    include_paths: frozenset[str] | None = None,
    exclude_paths: frozenset[str] = frozenset(),
) -> TreeSnapshot:
    """Read an already securely opened root; used by the trusted proc helper."""
    _require_posix()
    _check_limits(limits)
    entries: list[SnapshotEntry] = []
    count = 0
    total = 0
    seen: set[str] = set()
    included = None if include_paths is None else {
        "/".join(parts[:i]) for name in include_paths for parts in [_parts(name)]
        for i in range(1, len(parts) + 1)
    }

    def visit(fd: int, prefix: str) -> None:
        nonlocal count, total
        before_dir = os.fstat(fd)
        if not stat.S_ISDIR(before_dir.st_mode):
            raise SnapshotError("snapshot root is not a directory")
        with os.scandir(fd) as children:
            for child in children:
                name = f"{prefix}/{child.name}" if prefix else child.name
                if any(name == p or name.startswith(p + "/") for p in exclude_paths):
                    continue
                if included is not None and name not in included:
                    continue
                if workspace_input:
                    folded_name = child.name.casefold()
                    if (folded_name in (".git", ".codeagent", ".env")
                            or folded_name.startswith(".env.")):
                        continue
                count += 1
                if count > limits.max_files:
                    raise SnapshotError("file and directory count exceeds limit")
                _parts(name)
                folded = name.casefold()
                if folded in seen:
                    raise SnapshotError("case collision")
                seen.add(folded)
                before = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISDIR(before.st_mode):
                    sub_fd = os.open(child.name, _directory_flags(), dir_fd=fd)
                    try:
                        if not _same_file(before, os.fstat(sub_fd)):
                            raise SnapshotError("directory changed during snapshot")
                        visit(sub_fd, name)
                    finally:
                        os.close(sub_fd)
                elif stat.S_ISREG(before.st_mode) and before.st_nlink == 1:
                    remaining = min(limits.max_file_bytes, limits.max_total_bytes - total)
                    if before.st_size > remaining:
                        raise SnapshotError("file or total size exceeds limit")
                    flags = (os.O_RDONLY | _posix_flag("O_NOFOLLOW") | _posix_flag("O_CLOEXEC")
                             | _posix_flag("O_NONBLOCK"))
                    file_fd = os.open(child.name, flags, dir_fd=fd)
                    try:
                        if not _same_file(before, os.fstat(file_fd)):
                            raise SnapshotError("file changed during snapshot")
                        data = bytearray()
                        while True:
                            chunk = os.read(file_fd, min(65536, remaining - len(data) + 1))
                            if not chunk:
                                break
                            if len(data) + len(chunk) > remaining:
                                raise SnapshotError("file or total size exceeds limit")
                            data.extend(chunk)
                        if not _same_file(before, os.fstat(file_fd)) or len(data) != before.st_size:
                            raise SnapshotError("file changed during snapshot")
                    finally:
                        os.close(file_fd)
                    total += len(data)
                    entries.append(SnapshotEntry(name, bytes(data), bool(before.st_mode & 0o111)))
                else:
                    raise SnapshotError("links and special files are forbidden")
                after = os.stat(child.name, dir_fd=fd, follow_symlinks=False)
                if not _same_file(before, after):
                    raise SnapshotError("entry changed during snapshot")
        if not _same_file(before_dir, os.fstat(fd)):
            raise SnapshotError("directory changed during snapshot")

    visit(root_fd, "")
    snapshot = TreeSnapshot(tuple(sorted(entries, key=lambda e: e.path)))
    validate_snapshot(snapshot, limits)
    return snapshot


def read_tree(
    root: Path, limits: SnapshotLimits = SnapshotLimits(), *, workspace_input: bool = False,
    include_paths: frozenset[str] | None = None,
    exclude_paths: frozenset[str] = frozenset(),
) -> TreeSnapshot:
    """Read strictly; trusted initial input may omit repository state/secrets.

    Never set workspace_input when reading a container's output.
    """
    _check_limits(limits)
    try:
        fd = _open_directory(root)
        try:
            return _read_tree_fd(
                fd, limits, workspace_input=workspace_input,
                include_paths=include_paths, exclude_paths=exclude_paths,
            )
        finally:
            os.close(fd)
    except OSError as exc:
        raise SnapshotError("cannot securely read snapshot tree") from exc


def apply_snapshot(
    snapshot: TreeSnapshot, target: Path, limits: SnapshotLimits = SnapshotLimits(),
) -> None:
    """Materialize into an existing empty private staging directory; never delete."""
    validate_snapshot(snapshot, limits)
    try:
        fd = _open_directory(target)
        try:
            info = os.fstat(fd)
            geteuid = cast(Callable[[], int], getattr(os, "geteuid", None))
            fchmod = cast(Callable[[int, int], None], getattr(os, "fchmod", None))
            if info.st_uid != geteuid() or info.st_mode & 0o077:
                raise SnapshotError("target must be caller-owned and private (0700)")
            with os.scandir(fd) as children:
                if next(children, None) is not None:
                    raise SnapshotError("target must be empty")
            for entry in snapshot.entries:
                parts = _parts(entry.path)
                parent = os.dup(fd)
                try:
                    for part in parts[:-1]:
                        try:
                            os.mkdir(part, mode=0o700, dir_fd=parent)
                        except FileExistsError:
                            pass
                        child_fd = os.open(part, _directory_flags(), dir_fd=parent)
                        os.close(parent)
                        parent = child_fd
                    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL | _posix_flag("O_NOFOLLOW")
                             | _posix_flag("O_CLOEXEC"))
                    file_fd = os.open(parts[-1], flags, 0o600, dir_fd=parent)
                    try:
                        view = memoryview(entry.data)
                        while view:
                            written = os.write(file_fd, view)
                            if written <= 0:
                                raise SnapshotError("short snapshot write")
                            view = view[written:]
                        fchmod(file_fd, 0o700 if entry.executable else 0o600)
                    finally:
                        os.close(file_fd)
                finally:
                    os.close(parent)
        finally:
            os.close(fd)
    except OSError as exc:
        raise SnapshotError("cannot securely apply snapshot") from exc
