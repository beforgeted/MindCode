from __future__ import annotations

import asyncio
import os
from pathlib import Path

from codeagent.memory.models import IndexUpdate, MemoryItem, MemoryListQuery
from codeagent.memory.repository import MemoryRepository


class MemoryIndexProjector:
    def __init__(self, root: Path, repository: MemoryRepository, project_id: str) -> None:
        self._path = Path(root) / "memory" / "MEMORY.md"
        self._repository = repository
        self._project_id = project_id

    async def refresh_if_stale(self) -> IndexUpdate:
        content_revision, indexed_revision = await self._repository.revisions()
        if self._path.exists() and content_revision == indexed_revision:
            return IndexUpdate(True)
        try:
            items = await self._repository.list(
                MemoryListQuery(project_id=self._project_id, limit=200)
            )
            text = _render(items, self._project_id, content_revision)
            await asyncio.to_thread(self._write_atomic, text)
            await self._repository.mark_indexed(content_revision)
            return IndexUpdate(True)
        except Exception as exc:
            return IndexUpdate(False, f"MEMORY.md 索引更新失败: {exc}")

    def _write_atomic(self, text: str) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(".md.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)


def _render(items: list[MemoryItem], project_id: str, revision: int) -> str:
    lines = [
        "# Project Memory",
        "",
        "> Generated from SQLite. Do not edit; use `/memory` commands.",
        f"> Project: `{project_id}` · Revision: {revision}",
    ]
    grouped: dict[str, list[MemoryItem]] = {}
    for item in items:
        grouped.setdefault(str(item.type), []).append(item)
    for memory_type in sorted(grouped):
        lines.extend(("", f"## {memory_type.upper()}", ""))
        for item in sorted(grouped[memory_type], key=lambda x: (-x.updated_at.timestamp(), x.id)):
            preview = " ".join(item.content.split())[:240]
            refs = ", ".join(str(ref) for ref in item.evidence_refs) or "no evidence"
            lines.append(f"- `{item.id}` {preview}  ")
            lines.append(f"  Evidence: {refs}")
    lines.append("")
    return "\n".join(lines)
