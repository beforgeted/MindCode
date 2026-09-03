"""RawEventStore：入队不阻塞、退出前必须 drain。"""

from __future__ import annotations

import json

from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType


async def test_all_events_are_drained_on_close(home):
    store = JsonlEventStore(home, flush_interval_seconds=0.05, batch_size=8)
    await store.start()

    for index in range(200):
        store.append_nowait(
            AgentEvent(
                type=EventType.TOOL_CALL,
                session_id="ses_1",
                payload={"index": index},
            )
        )

    await store.aclose()

    path = home / "sessions" / "ses_1.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 200
    assert json.loads(lines[0])["payload"]["index"] == 0
    assert json.loads(lines[-1])["payload"]["index"] == 199


async def test_events_before_start_are_not_lost(home):
    store = JsonlEventStore(home)
    store.append_nowait(AgentEvent(type=EventType.USER_MESSAGE, session_id="ses_2"))
    store.append_nowait(AgentEvent(type=EventType.ASSISTANT_MESSAGE, session_id="ses_2"))
    await store.aclose()

    path = home / "sessions" / "ses_2.jsonl"
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2


async def test_query_filters_by_type(home):
    store = JsonlEventStore(home)
    await store.start()
    store.append_nowait(AgentEvent(type=EventType.USER_MESSAGE, session_id="ses_3"))
    store.append_nowait(AgentEvent(type=EventType.TOOL_CALL, session_id="ses_3"))
    store.append_nowait(AgentEvent(type=EventType.TOOL_RESULT, session_id="ses_3"))

    tool_events = await store.query("ses_3", types=[EventType.TOOL_CALL, EventType.TOOL_RESULT])
    assert [e.type for e in tool_events] == [EventType.TOOL_CALL, EventType.TOOL_RESULT]
    await store.aclose()


async def test_multiple_sessions_are_separated(home):
    store = JsonlEventStore(home, batch_size=2)
    await store.start()
    for index in range(10):
        store.append_nowait(
            AgentEvent(type=EventType.TOOL_CALL, session_id=f"ses_{index % 2}")
        )
    await store.aclose()

    for name in ("ses_0", "ses_1"):
        path = home / "sessions" / f"{name}.jsonl"
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        assert len(lines) == 5
