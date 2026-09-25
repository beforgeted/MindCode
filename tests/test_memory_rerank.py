from __future__ import annotations

from pathlib import Path

from codeagent.memory.models import MemoryScope, MemorySource, MemoryType, NewMemoryItem
from codeagent.memory.retriever import KeywordMemoryRetriever
from codeagent.memory.sqlite_store import SqliteMemoryStore


async def test_source_priority_ladder_ranks_user_explicit_above_assistant(tmp_path: Path):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        # 两条内容都命中 "Python"，但来源不同。
        assistant = await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.FACT,
                content="Python 可能用了某个虚拟环境",
                source=MemorySource.ASSISTANT_DERIVED,
            )
        )
        user = await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.CONSTRAINT,
                content="Python 版本必须固定 3.11",
                source=MemorySource.USER_EXPLICIT,
            )
        )
        retriever = KeywordMemoryRetriever(
            store,
            "p",
            source_weights={
                MemorySource.USER_EXPLICIT: 1.0,
                MemorySource.ASSISTANT_DERIVED: 0.3,
            },
        )
        ranked = await retriever.retrieve("Python")
        ids = [r.item.id for r in ranked]
        assert set(ids) == {assistant.id, user.id}
        # §29：用户显式记忆排在助手推导之前。
        assert ids.index(user.id) < ids.index(assistant.id)
    finally:
        await store.aclose()
