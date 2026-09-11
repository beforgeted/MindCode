from __future__ import annotations

from pathlib import Path

from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.memory.models import (
    MemoryListQuery,
    MemoryScope,
    MemorySearchQuery,
    MemorySource,
    MemoryStatus,
    MemoryType,
    NewMemoryItem,
)
from codeagent.memory.sqlite_store import SqliteMemoryStore


async def test_sqlite_memory_crud_search_and_soft_delete(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    draft = NewMemoryItem(
        project_id="project-a",
        scope=MemoryScope.PROJECT,
        scope_id="project-a",
        type=MemoryType.CONSTRAINT,
        content="项目固定使用 Python 3.11，并发任务必须保持工具协议",
        source=MemorySource.USER_EXPLICIT,
        tags=("python", "runtime"),
        evidence_refs=(EvidenceRef(EvidenceType.MESSAGE, event_id="evt_1"),),
    )

    item = await store.create(draft, event_id="evt_1")
    restored = await store.get("project-a", item.id)
    assert restored is not None
    assert restored == item
    assert restored.tags == ("python", "runtime")
    assert restored.evidence_refs[0].event_id == "evt_1"
    assert await store.list(MemoryListQuery("project-a")) == [item]
    assert (await store.search(MemorySearchQuery("project-a", "Python")))[0].item.id == item.id
    assert (await store.search(MemorySearchQuery("project-a", "并发")))[0].item.id == item.id
    assert await store.search(MemorySearchQuery("project-b", "Python")) == []

    deleted = await store.soft_delete("project-a", item.id)
    assert deleted.item.status is MemoryStatus.DELETED
    assert deleted.item.version == 2
    assert await store.get("project-a", item.id) is None
    assert (await store.get("project-a", item.id, include_deleted=True)) is not None
    assert await store.search(MemorySearchQuery("project-a", "Python")) == []
    again = await store.soft_delete("project-a", item.id)
    assert again.already_deleted
    assert again.item.version == 2
    await store.aclose()


async def test_sqlite_initialize_is_idempotent(tmp_path: Path):
    path = tmp_path / "memory.db"
    first = SqliteMemoryStore(path)
    await first.start()
    await first.aclose()
    second = SqliteMemoryStore(path)
    await second.start()
    assert await second.revisions() == (0, 0)
    await second.aclose()


async def test_like_wildcards_are_literal(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.FACT,
                content="literal percent % and underscore _",
                source=MemorySource.USER_EXPLICIT,
            )
        )
        assert (await store.search(MemorySearchQuery("p", "%")))[0].item.content.startswith(
            "literal"
        )
        assert (await store.search(MemorySearchQuery("p", "_")))[0].item.content.startswith(
            "literal"
        )
    finally:
        await store.aclose()


async def test_artifact_evidence_survives_database_reopen(tmp_path: Path):
    database = tmp_path / "memory.db"
    artifact_uri = "artifact://tool-output/art_test_001"
    evidence = EvidenceRef(EvidenceType.ARTIFACT, artifact_uri=artifact_uri)
    first = SqliteMemoryStore(database)
    await first.start()
    try:
        created = await first.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.TOOL_INSIGHT,
                content="完整工具输出保存在 artifact 中",
                source=MemorySource.TOOL_VERIFIED,
                evidence_refs=(evidence,),
            )
        )
    finally:
        await first.aclose()

    second = SqliteMemoryStore(database)
    await second.start()
    try:
        restored = await second.get("p", created.id)
        assert restored == created
        assert restored is not None
        assert restored.evidence_refs == (evidence,)
        restored_evidence = restored.evidence_refs[0]
        assert restored_evidence.type is EvidenceType.ARTIFACT
        assert restored_evidence.artifact_uri == artifact_uri
        assert restored_evidence.event_id is None
        assert restored_evidence.session_id is None
        assert restored_evidence.agent_run_id is None
        assert restored_evidence.tool_run_id is None
    finally:
        await second.aclose()
