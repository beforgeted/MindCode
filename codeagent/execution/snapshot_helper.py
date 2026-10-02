"""Trusted stdlib-only host helper: python3 -I snapshot_helper.py PID START_TIME.

The caller validates container identity/labels and paused state before and after
execution. The only followed symlink is the kernel-provided /proc/PID/root link;
workspace itself is opened O_NOFOLLOW and must be a distinct mount. No container
or project executable/module is loaded, even when the current directory is hostile.
"""
from __future__ import annotations

import importlib.util
import os
import stat
import sys
from pathlib import Path


def _load_snapshot_module():
    # __file__ belongs to the trusted host deployment, not /workspace or cwd.
    path = Path(__file__).resolve().with_name("snapshot.py")
    spec = importlib.util.spec_from_file_location("_trusted_workspace_snapshot", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load trusted snapshot module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _read_small(fd: int, maximum: int = 65536) -> bytes:
    data = bytearray()
    while True:
        chunk = os.read(fd, min(4096, maximum - len(data) + 1))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
        if len(data) > maximum:
            raise RuntimeError("oversized proc metadata")


def _posix_flag(name: str) -> int:
    # main() calls the trusted module's platform guard before resolving any flag.
    return int(getattr(os, name))


def _start_time(proc_fd: int) -> str:
    flags = os.O_RDONLY | _posix_flag("O_NOFOLLOW") | _posix_flag("O_CLOEXEC")
    fd = os.open("stat", flags, dir_fd=proc_fd)
    try:
        data = _read_small(fd)
    finally:
        os.close(fd)
    # comm may contain spaces and parentheses. All fields after its final ')' are numeric
    # except field 3 (state); field 22 is the process start time in clock ticks.
    _, separator, rest = data.rpartition(b")")
    fields = rest.split()
    if not separator or len(fields) < 20 or not fields[19].isdigit():
        raise RuntimeError("invalid proc stat")
    return fields[19].decode("ascii")


def _mount_id(fd: int) -> bytes:
    # This absolute path is in the trusted helper's proc filesystem, never the container.
    flags = os.O_RDONLY | _posix_flag("O_NOFOLLOW") | _posix_flag("O_CLOEXEC")
    info_fd = os.open(f"/proc/self/fdinfo/{fd}", flags)
    try:
        data = _read_small(info_fd)
    finally:
        os.close(info_fd)
    for line in data.splitlines():
        if line.startswith(b"mnt_id:"):
            value = line.split(b":", 1)[1].strip()
            if value.isdigit():
                return value
    raise RuntimeError("missing mount identity")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if (len(args) != 2 or not all(a.isascii() and a.isdecimal() for a in args)
                or args[0].startswith("0") or str(int(args[1])) != args[1]):
            raise RuntimeError("expected canonical positive PID and start time")
        snapshot = _load_snapshot_module()
        snapshot._require_posix()
        flags = (os.O_RDONLY | _posix_flag("O_DIRECTORY") | _posix_flag("O_NOFOLLOW")
                 | _posix_flag("O_CLOEXEC"))
        proc_root = os.open("/proc", flags)
        try:
            proc_fd = os.open(args[0], flags, dir_fd=proc_root)
        finally:
            os.close(proc_root)
        try:
            if _start_time(proc_fd) != args[1]:
                raise RuntimeError("process identity changed")
            # Intentionally follow this one kernel magic link to the container root.
            root_flags = os.O_RDONLY | _posix_flag("O_DIRECTORY") | _posix_flag("O_CLOEXEC")
            root_fd = os.open("root", root_flags, dir_fd=proc_fd)
            try:
                workspace_fd = os.open("workspace", flags, dir_fd=root_fd)
                try:
                    if (not stat.S_ISDIR(os.fstat(workspace_fd).st_mode)
                            or _mount_id(workspace_fd) == _mount_id(root_fd)):
                        raise RuntimeError("workspace is not a directory mountpoint")
                    limits = snapshot.SnapshotLimits()
                    tree = snapshot._read_tree_fd(workspace_fd, limits)
                    payload = snapshot.encode_snapshot(tree, limits)
                    current = os.stat("workspace", dir_fd=root_fd, follow_symlinks=False)
                    if not snapshot._same_file(current, os.fstat(workspace_fd)):
                        raise RuntimeError("workspace mount changed")
                    # Reopening also detects replacement with a different bind mount of
                    # the same inode (st_dev/st_ino alone cannot distinguish that).
                    check_fd = os.open("workspace", flags, dir_fd=root_fd)
                    try:
                        if _mount_id(check_fd) != _mount_id(workspace_fd):
                            raise RuntimeError("workspace mount changed")
                    finally:
                        os.close(check_fd)
                finally:
                    os.close(workspace_fd)
            finally:
                os.close(root_fd)
            if _start_time(proc_fd) != args[1]:
                raise RuntimeError("process identity changed")
        finally:
            os.close(proc_fd)
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"snapshot helper refused: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
