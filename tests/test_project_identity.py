from __future__ import annotations

import os
from pathlib import Path

import pytest

from codeagent.workspace.project_identity import (
    ProjectIdentityError,
    resolve_project_identity,
)


def test_project_identity_is_persistent(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    first = resolve_project_identity(workspace)
    second = resolve_project_identity(workspace)

    assert first.project_id == second.project_id
    assert first.project_id.startswith("prj_")
    assert first.marker_path.exists()


def test_explicit_project_identity_is_validated(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    identity = resolve_project_identity(workspace, explicit_id="project_ci-1")
    assert identity.project_id == "project_ci-1"
    with pytest.raises(ProjectIdentityError):
        resolve_project_identity(workspace, explicit_id="bad/project")


def test_marker_survives_workspace_move(tmp_path: Path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    project_id = resolve_project_identity(source).project_id
    os.replace(source, target)
    assert resolve_project_identity(target).project_id == project_id
