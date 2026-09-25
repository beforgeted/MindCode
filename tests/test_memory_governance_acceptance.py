from __future__ import annotations

from pathlib import Path

from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.memory.governance_service import MemoryGovernanceService
from codeagent.memory.judge import FakeMemoryJudge, JudgeVerdict
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

_CONSTRAINT_JAVA = JudgeVerdict(
    should_remember=True,
    scope=MemoryScope.PROJECT,
    type=MemoryType.CONSTRAINT,
    content="",
    importance=9,
    confidence=0.98,
)
_TEMPORARY = JudgeVerdict(should_remember=False, rationale="temporary task")
_ASSISTANT_GUESS = JudgeVerdict(should_remember=False, rationale="assistant speculation")


def _user(session: str, text: str) -> AgentEvent:
    return AgentEvent(type=EventType.USER_MESSAGE, session_id=session, payload={"text": text})


def _assistant(session: str, text: str) -> AgentEvent:
    return AgentEvent(type=EventType.ASSISTANT_MESSAGE, session_id=session, payload={"text": text})


async def _service(tmp_path: Path, judge: FakeMemoryJudge):
    events = JsonlEventStore(tmp_path)
    await events.start()
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    service = MemoryGovernanceService(
        event_store=events,
        repository=store,
        judge=judge,
        project_id="p",
    )
    return events, store, service


async def test_47_3_user_constraint_persists_across_sessions(tmp_path: Path):
    judge = FakeMemoryJudge({"Java": _CONSTRAINT_JAVA})
    events, store, service = await _service(tmp_path, judge)
    try:
        events.append_nowait(_user("s1", "项目 Java 版本必须固定在 17，不允许升级"))
        await events.flush()
        await service.run("s1")

        items = await store.list(MemoryListQuery("p", type=MemoryType.CONSTRAINT))
        assert len(items) == 1
        assert items[0].source is MemorySource.USER_EXPLICIT
        assert items[0].status is MemoryStatus.ACTIVE
        # 跨 Session（PROJECT scope）仍可检索到。
        hits = await store.search(MemorySearchQuery("p", "Java"))
        assert any(h.item.id == items[0].id for h in hits)
    finally:
        await store.aclose()
        await events.aclose()


async def test_47_4_temporary_task_not_persisted(tmp_path: Path):
    judge = FakeMemoryJudge({"debug": _TEMPORARY})
    events, store, service = await _service(tmp_path, judge)
    try:
        events.append_nowait(_user("s1", "这次先把日志级别调成 debug"))
        await events.flush()
        report = await service.run("s1")
        assert report[1].skipped == 1
        assert await store.list(MemoryListQuery("p")) == []
    finally:
        await store.aclose()
        await events.aclose()


async def test_47_5_assistant_guess_not_promoted(tmp_path: Path):
    judge = FakeMemoryJudge({"可能是因为": _ASSISTANT_GUESS})
    events, store, service = await _service(tmp_path, judge)
    try:
        events.append_nowait(_assistant("s1", "这个报错可能是因为历史上依赖版本冲突导致的"))
        await events.flush()
        report = await service.run("s1")
        assert report[1].promoted == 0
        assert await store.list(MemoryListQuery("p")) == []
    finally:
        await store.aclose()
        await events.aclose()


async def test_47_6_conflict_supersedes_old_version(tmp_path: Path):
    judge = FakeMemoryJudge({"Java": _CONSTRAINT_JAVA})
    events, store, service = await _service(tmp_path, judge)
    try:
        old = await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.CONSTRAINT,
                content="项目 Java 版本固定在 17，不升级",
                source=MemorySource.USER_EXPLICIT,
            )
        )
        events.append_nowait(_user("s1", "项目 Java 版本现在升级到 21"))
        await events.flush()
        report = await service.run("s1")
        assert report[1].superseded == 1

        refreshed_old = await store.get("p", old.id, include_deleted=True)
        assert refreshed_old is not None
        assert refreshed_old.status is MemoryStatus.SUPERSEDED

        active = await store.list(MemoryListQuery("p", type=MemoryType.CONSTRAINT))
        assert len(active) == 1
        assert "21" in active[0].content
        assert active[0].status is MemoryStatus.ACTIVE
    finally:
        await store.aclose()
        await events.aclose()
