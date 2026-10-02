"""Snapshot protocol tests; filesystem tests need real POSIX nofollow/dirfd support."""
from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from codeagent.execution import snapshot_helper
from codeagent.execution.snapshot import (
    SnapshotEntry,
    SnapshotError,
    SnapshotLimits,
    TreeSnapshot,
    apply_snapshot,
    decode_snapshot,
    encode_snapshot,
    read_tree,
    validate_snapshot,
)

POSIX = os.name == "posix" and hasattr(os, "O_NOFOLLOW")
requires_posix = pytest.mark.skipif(not POSIX, reason="requires POSIX nofollow traversal")


def tree(*paths: str) -> TreeSnapshot:
    return TreeSnapshot(tuple(SnapshotEntry(path, b"content") for path in paths))


def payload(**updates: object) -> bytes:
    obj = {"version": 1, "entries": [{"path": "a", "data": "YQ==", "executable": False}]}
    obj.update(updates)
    return json.dumps(obj).encode()


def test_roundtrip_and_frozen_contract() -> None:
    snapshot = TreeSnapshot((SnapshotEntry("src/a.py", b"\x00\xff\n", True),
                             SnapshotEntry("README", b"")))
    assert decode_snapshot(encode_snapshot(snapshot)) == snapshot
    assert SnapshotLimits() == SnapshotLimits(4096, 8 * 1024 * 1024, 64 * 1024 * 1024)
    for value, field in [(snapshot, "entries"), (snapshot.entries[0], "path"),
                         (SnapshotLimits(), "max_files")]:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, field, None)


@pytest.mark.parametrize("path", [
    "", "/abs", "//server/path", "a/", "a//b", ".", "..", "a/../b", "a/./b",
    "a\\b", "C:/a", "C:a", "a\x00b", ".git", ".GiT/config", "a/.git/b",
    ".codeagent", "a/.CODEAGENT/config", ".env", ".ENV.local", "a/.env.prod",
    "a/" * 128 + "b", "x" * 4097, "\ud800",
])
def test_invalid_paths(path: str) -> None:
    with pytest.raises(SnapshotError):
        validate_snapshot(tree(path))
    with pytest.raises(SnapshotError):
        encode_snapshot(tree(path))
    with pytest.raises(SnapshotError):
        decode_snapshot(payload(entries=[{"path": path, "data": "", "executable": False}]))


@pytest.mark.parametrize("paths", [
    ("a", "a"), ("a", "A"), ("a", "a/b"), ("a/b", "a"),
    ("A/b", "a/c"), ("a/b", "A"), ("straße", "STRASSE"),
])
def test_collisions(paths: tuple[str, ...]) -> None:
    with pytest.raises(SnapshotError):
        validate_snapshot(tree(*paths))
    with pytest.raises(SnapshotError):
        decode_snapshot(payload(entries=[{"path": p, "data": "", "executable": False}
                                         for p in paths]))


def test_shared_directories_and_unprotected_dotfiles() -> None:
    validate_snapshot(tree("a/b", "a/c", ".gitignore", ".environment", "env/.envrc"))


@pytest.mark.parametrize("raw", [
    b"null", b"[]", b"{}", b"not json", b"\xff", b'{"version":1,"version":1,"entries":[]}',
    b'{"version":1,"entries":[{"path":"a","path":"b","data":"","executable":false}]}',
    payload(version=True), payload(version=1.0), payload(version=2), payload(extra=1),
    payload(entries={}), payload(entries=[None]), payload(entries=[{}]),
])
def test_malformed_protocol(raw: bytes) -> None:
    with pytest.raises(SnapshotError):
        decode_snapshot(raw)


@pytest.mark.parametrize(("field", "value"), [
    ("path", 1), ("path", None), ("data", 1), ("data", None), ("data", "%%%%"),
    ("data", "YQ==\n"), ("data", "YQ==="), ("data", "YR=="), ("data", "é"),
    ("executable", 1), ("executable", "false"), ("executable", None),
])
def test_entry_type_and_base64_checks(field: str, value: object) -> None:
    entry = {"path": "a", "data": "YQ==", "executable": False}
    entry[field] = value
    with pytest.raises(SnapshotError):
        decode_snapshot(payload(entries=[entry]))


@pytest.mark.parametrize("snapshot", [
    None, TreeSnapshot(cast(Any, [])), TreeSnapshot(cast(Any, (None,))),
    TreeSnapshot((SnapshotEntry("a", cast(Any, "text")),)),
    TreeSnapshot((SnapshotEntry("a", cast(Any, bytearray())),)),
    TreeSnapshot((SnapshotEntry("a", b"", cast(Any, 1)),)),
])
def test_dataclass_runtime_types(snapshot: TreeSnapshot) -> None:
    with pytest.raises(SnapshotError):
        validate_snapshot(snapshot)


@pytest.mark.parametrize("limits", [
    SnapshotLimits(max_files=True), SnapshotLimits(max_file_bytes=-1),
    SnapshotLimits(max_total_bytes=cast(Any, 1.0)), None,
])
def test_invalid_limits(limits: SnapshotLimits) -> None:
    with pytest.raises(SnapshotError):
        validate_snapshot(TreeSnapshot(()), limits)


def test_limits_count_bytes_and_implicit_directories() -> None:
    exact = TreeSnapshot((SnapshotEntry("a/b", b"123"), SnapshotEntry("a/c", b"45")))
    limits = SnapshotLimits(max_files=3, max_file_bytes=3, max_total_bytes=5)
    assert decode_snapshot(encode_snapshot(exact, limits), limits) == exact
    for too_small in [dataclasses.replace(limits, max_files=2),
                      dataclasses.replace(limits, max_file_bytes=2),
                      dataclasses.replace(limits, max_total_bytes=4)]:
        with pytest.raises(SnapshotError):
            validate_snapshot(exact, too_small)
        with pytest.raises(SnapshotError):
            decode_snapshot(encode_snapshot(exact), too_small)
    empty = TreeSnapshot(())
    zero = SnapshotLimits(0, 0, 0)
    assert decode_snapshot(encode_snapshot(empty, zero), zero) == empty
    with pytest.raises(SnapshotError):
        decode_snapshot(b" " * 129, zero)


@pytest.mark.skipif(POSIX, reason="unsupported-platform behavior")
def test_unsupported_platform_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError, match="unsupported"):
        read_tree(tmp_path)
    with pytest.raises(SnapshotError, match="unsupported"):
        apply_snapshot(tree("a"), tmp_path)


@requires_posix
def test_filesystem_roundtrip_executable(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir(mode=0o700)
    (source / "src").mkdir()
    (source / "src" / "run").write_bytes(b"binary\x00\xff")
    (source / "src" / "run").chmod(0o751)
    (source / "plain").write_bytes(b"abc")
    (source / "plain").chmod(0o600)
    snapshot = read_tree(source)
    assert snapshot == TreeSnapshot((SnapshotEntry("plain", b"abc"),
                                    SnapshotEntry("src/run", b"binary\x00\xff", True)))
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    apply_snapshot(decode_snapshot(encode_snapshot(snapshot)), target)
    assert read_tree(target) == snapshot


@requires_posix
@pytest.mark.parametrize("name", [".git", ".GIT", ".codeagent", ".env", ".Env.local"])
@pytest.mark.parametrize("directory", [False, True])
def test_read_rejects_protected_entries(tmp_path: Path, name: str, directory: bool) -> None:
    path = tmp_path / name
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b"secret")
    with pytest.raises(SnapshotError):
        read_tree(tmp_path)


@requires_posix
@pytest.mark.parametrize("kind", ["file_symlink", "dir_symlink", "dangling", "hardlink", "fifo"])
def test_read_rejects_links_and_special(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    source.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"secret")
    bad = source / "bad"
    if kind == "file_symlink":
        bad.symlink_to(outside)
    elif kind == "dir_symlink":
        bad.symlink_to(tmp_path, target_is_directory=True)
    elif kind == "dangling":
        bad.symlink_to(tmp_path / "missing")
    elif kind == "hardlink":
        os.link(outside, bad)
    else:
        mkfifo = cast(Callable[[Path], None], getattr(os, "mkfifo", None))
        mkfifo(bad)
    with pytest.raises(SnapshotError):
        read_tree(source)


@requires_posix
def test_linked_root_and_ancestor_rejected(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (real / "child").mkdir(mode=0o700)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    for root in [linked, linked / "child"]:
        with pytest.raises(SnapshotError):
            read_tree(root)
        with pytest.raises(SnapshotError):
            apply_snapshot(tree("a"), root)
    assert list((real / "child").iterdir()) == []


@requires_posix
def test_read_count_includes_empty_directories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    with pytest.raises(SnapshotError):
        read_tree(tmp_path, SnapshotLimits(max_files=1))
    assert read_tree(tmp_path, SnapshotLimits(max_files=2)) == TreeSnapshot(())


@requires_posix
def test_read_size_limits(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"123")
    (tmp_path / "b").write_bytes(b"456")
    for limits in [SnapshotLimits(max_file_bytes=2), SnapshotLimits(max_total_bytes=5)]:
        with pytest.raises(SnapshotError):
            read_tree(tmp_path, limits)


@requires_posix
@pytest.mark.parametrize("kind", ["file", "dir", "symlink", "protected"])
def test_apply_nonempty_refusal(tmp_path: Path, kind: str) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    existing = target / (".git" if kind == "protected" else "existing")
    if kind == "dir":
        existing.mkdir()
    elif kind == "symlink":
        existing.symlink_to(tmp_path / "missing")
    else:
        existing.write_bytes(b"keep")
    with pytest.raises(SnapshotError):
        apply_snapshot(tree("new"), target)
    assert not (target / "new").exists()
    assert existing.exists() or existing.is_symlink()


@requires_posix
def test_apply_validates_before_writing_and_checks_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    with pytest.raises(SnapshotError):
        apply_snapshot(tree("valid", ".env"), target)
    assert list(target.iterdir()) == []
    target.chmod(0o755)
    with pytest.raises(SnapshotError, match="private"):
        apply_snapshot(tree("valid"), target)
    assert list(target.iterdir()) == []


@requires_posix
def test_host_control_directory_is_trusted_anchor(tmp_path: Path) -> None:
    host_control = tmp_path / ".codeagent"
    host_control.mkdir(mode=0o700)
    source = host_control / "source"
    source.mkdir(mode=0o700)
    (source / "safe").write_bytes(b"data")
    snapshot = read_tree(source)
    assert snapshot == TreeSnapshot((SnapshotEntry("safe", b"data"),))
    target = host_control / "stage"
    target.mkdir(mode=0o700)
    apply_snapshot(snapshot, target)
    assert read_tree(target) == snapshot
    (source / ".env").write_bytes(b"secret")
    with pytest.raises(SnapshotError, match="protected"):
        read_tree(source)
    empty = host_control / "empty"
    empty.mkdir(mode=0o700)
    with pytest.raises(SnapshotError, match="protected"):
        apply_snapshot(tree(".codeagent/config"), empty)
    assert list(empty.iterdir()) == []
    linked = host_control / "linked"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(SnapshotError):
        read_tree(linked)
    with pytest.raises(SnapshotError):
        apply_snapshot(tree("safe"), linked)


def test_helper_loads_fixed_sibling_not_cwd(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "snapshot.py").write_text("raise AssertionError('untrusted code loaded')")
    monkeypatch.chdir(tmp_path)
    module = snapshot_helper._load_snapshot_module()
    assert isinstance(module.__file__, str)
    assert Path(module.__file__).resolve() == Path(snapshot_helper.__file__).with_name(
        "snapshot.py").resolve()
    assert module.decode_snapshot(module.encode_snapshot(module.TreeSnapshot(()))).entries == ()


@pytest.mark.parametrize("args", [[], ["1"], ["-1", "1"], ["0", "1"], ["01", "1"],
                                 ["1", "01"], ["1", "a"], ["١", "1"], ["1", "1", "x"]])
def test_helper_rejects_bad_arguments(args: list[str], capsys) -> None:
    assert snapshot_helper.main(args) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refused" in captured.err


def test_helper_proc_starttime_parsing(monkeypatch) -> None:
    # comm contains whitespace and ')' so naive whitespace parsing is unsafe.
    data = b"123 (worker ) (name)) S " + b" ".join([b"0"] * 18 + [b"987654", b"0"])
    for name in ["O_NOFOLLOW", "O_CLOEXEC"]:
        monkeypatch.setattr(snapshot_helper.os, name, 0, raising=False)
    monkeypatch.setattr(snapshot_helper.os, "open", lambda *a, **kw: 42)
    monkeypatch.setattr(snapshot_helper.os, "close", lambda fd: None)
    monkeypatch.setattr(snapshot_helper, "_read_small", lambda fd: data)
    assert snapshot_helper._start_time(10) == "987654"


def test_helper_rejects_changed_starttime(monkeypatch, capsys) -> None:
    module = snapshot_helper._load_snapshot_module()
    monkeypatch.setattr(module, "_require_posix", lambda: None)
    monkeypatch.setattr(snapshot_helper, "_load_snapshot_module", lambda: module)
    # Flags do not exist on Windows; fake flags only for this metadata-order test.
    for name in ["O_DIRECTORY", "O_NOFOLLOW", "O_CLOEXEC"]:
        monkeypatch.setattr(snapshot_helper.os, name, 0, raising=False)
    opened = []

    def fake_open(path, *args, **kwargs):
        opened.append(path)
        return 42

    monkeypatch.setattr(snapshot_helper.os, "open", fake_open)
    monkeypatch.setattr(snapshot_helper.os, "close", lambda fd: None)
    monkeypatch.setattr(snapshot_helper, "_start_time", lambda fd: "2")
    assert snapshot_helper.main(["1", "1"]) == 1
    assert opened == ["/proc", "1"]  # Never access the workspace after identity mismatch.
    assert "identity changed" in capsys.readouterr().err
