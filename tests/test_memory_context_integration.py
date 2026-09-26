from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from codeagent.context.compact.base import CompactionResult
from codeagent.context.compact.models import TaskCheckpoint
from codeagent.context.history.conversation_history import ConversationHistory
from codeagent.context.manager import ContextManager
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.infra import metrics as M
from codeagent.infra.metrics import Metrics
from codeagent.llm.message import ContextCategory, Message, Role
from codeagent.memory.models import (
    MemoryItem,
    MemoryScope,
    MemorySearchQuery,
    MemorySource,
    MemoryStatus,
    MemoryType,
    NewMemoryItem,
)
from codeagent.memory.retriever import KeywordMemoryRetriever, RankedMemory
from codeagent.memory.sqlite_store import SqliteMemoryStore


class StaticRetriever:
    def __init__(self, items: list[MemoryItem]) -> None:
        self.items = items
        self.calls = 0

    async def retrieve(self, query, *, checkpoint=None, limit=50, type_filter=None):
        self.calls += 1
        return [RankedMemory(item, 1.0) for item in self.items[:limit]]


class DeterministicCompactor:
    def __init__(self, current_turn_id: str) -> None:
        self.current_turn_id = current_turn_id

    async def compact(self, messages, **kwargs):
        checkpoint = TaskCheckpoint(goal="keep working")
        candidate = tuple(
            [message for message in messages if message.role is Role.SYSTEM]
            + [checkpoint.to_message()]
            + [message for message in messages if message.turn_id == self.current_turn_id]
        )
        return CompactionResult(
            compacted=True,
            messages=candidate,
            checkpoint=checkpoint,
            tokens_before=1_000,
            tokens_after=100,
        )


class TimeoutRetriever:
    async def retrieve(self, query, *, checkpoint=None, limit=50, type_filter=None):
        await asyncio.Event().wait()
        return ()


class FailingRetriever:
    async def retrieve(self, query, *, checkpoint=None, limit=50, type_filter=None):
        raise RuntimeError("simulated retrieval failure")


def _item(content: str) -> MemoryItem:
    now = datetime.now(UTC)
    return MemoryItem(
        id="mem_1",
        project_id="p",
        scope=MemoryScope.PROJECT,
        scope_id="p",
        type=MemoryType.CONSTRAINT,
        content=content,
        source=MemorySource.USER_EXPLICIT,
        status=MemoryStatus.ACTIVE,
        evidence_refs=(),
        tags=(),
        confidence=None,
        importance=None,
        created_at=now,
        updated_at=now,
    )


async def test_memory_is_request_local_before_current_user():
    history = ConversationHistory(session_id="s", agent_run_id="r")
    history.append(Message.system("system"))
    turn_id = history.begin_turn()
    history.append(Message.user("请检查 Python 版本", turn_id=turn_id))
    source = history.messages
    retriever = StaticRetriever([_item("项目固定使用 Python 3.11")])
    manager = ContextManager(memory_retriever=retriever)

    first = await manager.prepare(history, ContextProfile())
    second = await manager.prepare(history, ContextProfile())

    assert first.messages[1].category is ContextCategory.MEMORY
    assert first.messages[2].text == "请检查 Python 版本"
    assert history.messages == source
    assert sum(m.category is ContextCategory.MEMORY for m in second.messages) == 1
    assert first.memory_selected == 1
    assert first.memory_tokens > 0


async def test_memory_is_not_retrieved_without_active_turn():
    history = ConversationHistory(session_id="s", agent_run_id="r")
    retriever = StaticRetriever([_item("anything")])
    result = await ContextManager(memory_retriever=retriever).prepare(
        history, ContextProfile(), force_compact=True
    )
    assert retriever.calls == 0
    assert result.memory_selected == 0


async def test_memory_over_budget_is_skipped():
    history = ConversationHistory(session_id="s", agent_run_id="r")
    turn_id = history.begin_turn()
    history.append(Message.user("query", turn_id=turn_id))
    retriever = StaticRetriever([_item("x" * 100_000)])
    profile = ContextProfile(max_memory_injection_tokens=100)
    result = await ContextManager(
        estimator=HeuristicTokenEstimator(), memory_retriever=retriever
    ).prepare(history, profile)
    assert result.memory_candidates == 1
    assert result.memory_selected == 0
    assert all(message.category is not ContextCategory.MEMORY for message in result.messages)


def _completed_turn(history: ConversationHistory, text: str) -> None:
    turn_id = history.begin_turn()
    history.append(Message.user(text, turn_id=turn_id))
    history.append(Message.assistant((), turn_id=turn_id))
    history.end_turn()


async def test_compaction_keeps_durable_memory_retrievable_and_request_local(
    tmp_path: Path,
):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        item = await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.CONSTRAINT,
                content="项目固定使用 Python 3.11",
                source=MemorySource.USER_EXPLICIT,
            )
        )
        history = ConversationHistory(session_id="s", agent_run_id="r")
        history.append(Message.system("system"))
        _completed_turn(history, "old turn")
        current_turn_id = history.begin_turn()
        history.append(Message.user("请检查 Python 版本", turn_id=current_turn_id))
        manager = ContextManager(
            compactor=DeterministicCompactor(current_turn_id),
            memory_retriever=KeywordMemoryRetriever(store, "p"),
        )

        result = await manager.prepare(history, ContextProfile(), force_compact=True)

        assert result.compacted
        assert history.compaction_count == 1
        assert history.checkpoint is not None
        assert all(message.text != "old turn" for message in history.messages)
        hits = await store.search(MemorySearchQuery("p", "Python"))
        assert [hit.item.id for hit in hits] == [item.id]
        memory_messages = [
            message for message in result.messages if message.category is ContextCategory.MEMORY
        ]
        assert len(memory_messages) == 1
        assert item.id in memory_messages[0].text
        assert "项目固定使用 Python 3.11" in memory_messages[0].text
        memory_index = result.messages.index(memory_messages[0])
        user_index = next(
            index
            for index, message in enumerate(result.messages)
            if message.turn_id == current_turn_id and message.role is Role.USER
        )
        assert memory_index < user_index
        assert result.memory_candidates == 1
        assert result.memory_selected == 1
        assert result.memory_tokens > 0
        assert all(message.category is not ContextCategory.MEMORY for message in history.messages)
        assert tuple(
            message
            for message in result.messages
            if message.category is not ContextCategory.MEMORY
        ) == history.messages
    finally:
        await store.aclose()


async def test_memory_retrieval_timeout_degrades_without_blocking_request():
    history = ConversationHistory(session_id="s", agent_run_id="r")
    turn_id = history.begin_turn()
    history.append(Message.user("query", turn_id=turn_id))
    source = history.messages
    metrics = Metrics()
    profile = replace(ContextProfile(), memory_retrieval_timeout_seconds=0.01)

    result = await ContextManager(
        memory_retriever=TimeoutRetriever(), metrics=metrics
    ).prepare(history, profile)

    assert result.memory_degraded_reason == "Memory retrieval 超时，已跳过"
    assert result.memory_candidates == result.memory_selected == result.memory_tokens == 0
    assert all(message.category is not ContextCategory.MEMORY for message in result.messages)
    assert any(message.text == "query" for message in result.messages)
    assert history.messages == source
    assert metrics.counters[M.MEMORY_RETRIEVAL_TIMEOUTS] == 1
    assert metrics.counters.get(M.MEMORY_RETRIEVAL_FAILURES, 0) == 0
    assert metrics.snapshot()["histograms"][f"{M.MEMORY_RETRIEVAL_MS}.count"] == 1.0


async def test_memory_retrieval_failure_degrades_without_blocking_request():
    history = ConversationHistory(session_id="s", agent_run_id="r")
    turn_id = history.begin_turn()
    history.append(Message.user("query", turn_id=turn_id))
    source = history.messages
    metrics = Metrics()

    result = await ContextManager(
        memory_retriever=FailingRetriever(), metrics=metrics
    ).prepare(history, ContextProfile())

    assert result.memory_degraded_reason is not None
    assert "失败" in result.memory_degraded_reason
    assert "已跳过" in result.memory_degraded_reason
    assert "RuntimeError" in result.memory_degraded_reason
    assert result.memory_candidates == result.memory_selected == result.memory_tokens == 0
    assert all(message.category is not ContextCategory.MEMORY for message in result.messages)
    assert any(message.text == "query" for message in result.messages)
    assert history.messages == source
    assert metrics.counters[M.MEMORY_RETRIEVAL_FAILURES] == 1
    assert metrics.counters.get(M.MEMORY_RETRIEVAL_TIMEOUTS, 0) == 0
    assert metrics.snapshot()["histograms"][f"{M.MEMORY_RETRIEVAL_MS}.count"] == 1.0
