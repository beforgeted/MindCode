"""Real POSIX publication tests; skipped explicitly on Windows."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent.execution.snapshot import SnapshotEntry, SnapshotError, TreeSnapshot, read_tree
from codeagent.execution.workspace import capture_workspace, publish_workspace
from codeagent.workspace.context import WorkspaceContext

pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires POSIX workspace handoff")


def workspace(root: Path) -> WorkspaceContext:
    return WorkspaceContext(root=root, worktree_id="test", is_isolated=True)


def test_capture_omits_metadata_and_secrets_but_export_remains_strict(tmp_path):
    (tmp_path / ".git").write_text("gitdir: private")
    (tmp_path / ".env").write_text("secret")
    (tmp_path / ".codeagent").mkdir()
    (tmp_path / ".codeagent" / "runs.db").write_text("state")
    (tmp_path / "source.py").write_text("pass")
    initial = capture_workspace(workspace(tmp_path))
    assert initial == TreeSnapshot((SnapshotEntry("source.py", b"pass"),))
    with pytest.raises(SnapshotError):
        read_tree(tmp_path)
    publish_workspace(workspace(tmp_path), initial, TreeSnapshot(()))
    assert not (tmp_path / "source.py").exists()
    assert (tmp_path / ".env").read_text() == "secret"
    assert (tmp_path / ".git").read_text() == "gitdir: private"
    assert (tmp_path / ".codeagent" / "runs.db").read_text() == "state"


def test_publication_handles_deletes_mode_changes_and_file_directory_transitions(tmp_path):
    (tmp_path / "a").write_text("file to directory")
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "c").write_text("directory to file")
    (tmp_path / "delete").write_text("old")
    (tmp_path / "script").write_text("same")
    initial = capture_workspace(workspace(tmp_path))
    output = TreeSnapshot((SnapshotEntry("a/nested", b"new"), SnapshotEntry("b", b"new"),
                           SnapshotEntry("script", b"same", True)))
    publish_workspace(workspace(tmp_path), initial, output)
    assert read_tree(tmp_path) == output
    assert not (tmp_path / "delete").exists()
    assert tmp_path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("unsafe", ["../escape", ".git/config", ".env", "a/../../escape"])
def test_invalid_output_does_not_modify_worker(tmp_path, unsafe):
    (tmp_path / "keep").write_text("original")
    initial = capture_workspace(workspace(tmp_path))
    with pytest.raises(SnapshotError):
        output = TreeSnapshot((SnapshotEntry(unsafe, b"x"),))
        publish_workspace(workspace(tmp_path), initial, output)
    assert read_tree(tmp_path) == initial


def test_changed_worker_and_symlink_input_rejected(tmp_path):
    (tmp_path / "keep").write_text("original")
    initial = capture_workspace(workspace(tmp_path))
    (tmp_path / "keep").write_text("external change")
    with pytest.raises(SnapshotError, match="changed"):
        publish_workspace(workspace(tmp_path), initial, TreeSnapshot(()))
    assert (tmp_path / "keep").read_text() == "external change"
    (tmp_path / "link").symlink_to(tmp_path / "keep")
    with pytest.raises(SnapshotError):
        capture_workspace(workspace(tmp_path))


def test_shared_workspace_and_git_control_changes_rejected(tmp_path):
    shared = WorkspaceContext.local(tmp_path)
    with pytest.raises(SnapshotError):
        publish_workspace(shared, TreeSnapshot(()), TreeSnapshot(()))
    (tmp_path / ".gitattributes").write_text("*.txt text")
    initial = capture_workspace(workspace(tmp_path))
    with pytest.raises(SnapshotError, match="Git control"):
        publish_workspace(workspace(tmp_path), initial, TreeSnapshot(()))
    assert capture_workspace(workspace(tmp_path)) == initial
