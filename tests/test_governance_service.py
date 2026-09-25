from __future__ import annotations

from pathlib import Path

from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.memory.governance_service import MemoryGovernanceService
from codeagent.memory.sqlite_store import SqliteMemoryStore


def _user(session: str, text: str, *, channel: str | None = None) -> AgentEvent:
    payload: dict = {"text": text}
    if channel:
        payload["channel"] = channel
    return AgentEvent(type=EventType.USER_MESSAGE, session_id=session, payload=payload)


async def _make(tmp_path: Path):
    events = JsonlEventStore(tmp_path)
    await events.start()
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    service = MemoryGovernanceService(
        event_store=events,
        repository=store,
        project_id="p",
    )
    return events, store, service


async def test_harvest_stages_user_messages_and_advances_cursor(tmp_path: Path):
    events, store, service = await _make(tmp_path)
    try:
        events.append_nowait(_user("s", "项目固定使用 Python 3.11"))
        events.append_nowait(_user("s", "构建命令是 pip install -e ."))
        # memory_cli 通道不应被当作候选（避免 /memory add 回环）。
        events.append_nowait(_user("s", "手动记的", channel="memory_cli"))
        await events.flush()

        report = await service.harvest("s")
        assert report.staged == 2
        assert report.scanned == 3
        cursor = await store.governance_cursor("p", "s")
        assert cursor == 3
        pending = await store.list_pending_candidates("p", "s")
        assert {c.content for c in pending} == {
            "项目固定使用 Python 3.11",
            "构建命令是 pip install -e .",
        }
    finally:
        await store.aclose()
        await events.aclose()


async def test_harvest_is_idempotent_and_resumes(tmp_path: Path):
    events, store, service = await _make(tmp_path)
    try:
        events.append_nowait(_user("s", "第一条约束"))
        await events.flush()
        first = await service.harvest("s")
        assert first.staged == 1

        # 再来两条，第二次 harvest 从游标续跑，不重复处理旧事件。
        events.append_nowait(_user("s", "第二条约束"))
        events.append_nowait(_user("s", "第三条约束"))
        await events.flush()
        second = await service.harvest("s")
        assert second.staged == 2
        assert second.scanned == 2  # 只扫新事件
        assert await store.governance_cursor("p", "s") == 3
        assert len(await store.list_pending_candidates("p", "s")) == 3

        # 无新事件时 harvest 空跑。
        third = await service.harvest("s")
        assert third.staged == 0
        assert third.scanned == 0
    finally:
        await store.aclose()
        await events.aclose()


async def test_harvest_routes_sensitive_content_to_receipts(tmp_path: Path):
    events, store, service = await _make(tmp_path)
    try:
        events.append_nowait(_user("s", "api_key=sk-abcdefghijklmnop1234"))
        events.append_nowait(_user("s", "这是一条正常的项目约束说明"))
        await events.flush()
        report = await service.harvest("s")
        assert report.staged == 1
        assert report.receipts == 1
        pending = await store.list_pending_candidates("p", "s")
        assert len(pending) == 1
        assert "api_key" not in pending[0].content
    finally:
        await store.aclose()
        await events.aclose()
