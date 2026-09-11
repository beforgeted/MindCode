from __future__ import annotations

from pathlib import Path

import pytest

from codeagent.evidence.event_store import NullEventStore
from codeagent.memory.index_projector import MemoryIndexProjector
from codeagent.memory.models import MemoryStatus, MemoryType
from codeagent.memory.service import MemoryService
from codeagent.memory.sqlite_store import SqliteMemoryStore


async def test_memory_service_add_project_and_project_index(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    projector = MemoryIndexProjector(tmp_path, store, "project-a")
    service = MemoryService(
        store,
        projector,
        NullEventStore(),
        project_id="project-a",
        session_id="session-a",
    )
    try:
        startup = await service.start()
        assert startup.ok

        item, index = await service.add(
            "项目固定使用 Python 3.11",
            MemoryType.CONSTRAINT,
            tags=("Runtime", "runtime"),
        )
        assert item.project_id == "project-a"
        assert item.tags == ("runtime",)
        assert item.evidence_refs[0].event_id
        assert index.ok
        text = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert item.id in text
        assert "SQLite" in text

        listed = await service.list()
        assert [value.id for value in listed] == [item.id]
        assert (await service.search("Python"))[0].item.id == item.id
        deleted, update = await service.delete(item.id)
        assert deleted.item.status is MemoryStatus.DELETED
        assert update.ok
        assert await service.list() == []
    finally:
        await service.aclose()


async def test_projection_failure_keeps_db_and_next_start_rebuilds_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "memory.db"
    store = SqliteMemoryStore(database)
    service = MemoryService(
        store,
        MemoryIndexProjector(tmp_path, store, "project-a"),
        NullEventStore(),
        project_id="project-a",
        session_id="session-a",
    )
    item_id = ""
    try:
        assert (await service.start()).ok

        def fail_projection(self, text):
            raise OSError("simulated projection failure")

        with monkeypatch.context() as patch:
            patch.setattr(MemoryIndexProjector, "_write_atomic", fail_projection)
            item, update = await service.add(
                "项目固定使用 Python 3.11", MemoryType.CONSTRAINT
            )
            item_id = item.id
            assert not update.ok
            assert update.warning is not None
            assert "MEMORY.md 索引更新失败" in update.warning
            assert "simulated projection failure" in update.warning
            assert service.available
            assert service.unavailable_reason is None
            assert (await service.show(item.id)) == item
            assert [value.id for value in await service.list()] == [item.id]
            assert (await service.search("Python"))[0].item.id == item.id
            assert await store.revisions() == (1, 0)
    finally:
        await service.aclose()

    reopened_store = SqliteMemoryStore(database)
    reopened = MemoryService(
        reopened_store,
        MemoryIndexProjector(tmp_path, reopened_store, "project-a"),
        NullEventStore(),
        project_id="project-a",
        session_id="session-b",
    )
    try:
        startup = await reopened.start()
        assert startup.ok
        restored = await reopened.show(item_id)
        assert restored is not None
        assert restored.id == item_id
        assert (await reopened.search("Python"))[0].item.id == item_id
        assert await reopened_store.revisions() == (1, 1)
        text = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
        assert item_id in text
        assert "项目固定使用 Python 3.11" in text
        assert "Revision: 1" in text
    finally:
        await reopened.aclose()
