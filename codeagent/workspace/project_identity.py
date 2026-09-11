from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass(frozen=True, slots=True)
class ProjectIdentity:
    project_id: str
    marker_path: Path


class ProjectIdentityError(ValueError):
    pass


def resolve_project_identity(
    workspace_root: Path,
    *,
    explicit_id: str | None = None,
) -> ProjectIdentity:
    workspace = Path(workspace_root).resolve()
    marker = workspace / ".codeagent" / "project.json"
    if explicit_id:
        return ProjectIdentity(_validate_project_id(explicit_id), marker)
    try:
        return ProjectIdentity(_read_marker(marker), marker)
    except FileNotFoundError:
        pass

    marker.parent.mkdir(parents=True, exist_ok=True)
    project_id = f"prj_{uuid4().hex}"
    payload = json.dumps(
        {
            "schema_version": 1,
            "project_id": project_id,
            "created_at": datetime.now(UTC).isoformat(),
        },
        ensure_ascii=False,
        indent=2,
    )
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return ProjectIdentity(_read_marker(marker), marker)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        marker.unlink(missing_ok=True)
        raise
    return ProjectIdentity(project_id, marker)


def _read_marker(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise ProjectIdentityError(f"不支持的 project marker 版本: {data.get('schema_version')}")
    return _validate_project_id(data.get("project_id"))


def _validate_project_id(value: object) -> str:
    if not isinstance(value, str) or not _PROJECT_ID_RE.fullmatch(value):
        raise ProjectIdentityError("project_id 必须匹配 [A-Za-z0-9._-]{1,64}")
    return value
