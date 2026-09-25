from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from codeagent.evidence.cursor import EventBatch, EventCursor
from codeagent.evidence.models import AgentEvent, EventType


@runtime_checkable
class RawEventStore(Protocol):
    def append_nowait(self, event: AgentEvent) -> None:
        """同步入队，不阻塞调用方。

        ConversationHistory.append() 是同步的，而事件落盘是 I/O，
        所以这里必须是 fire-and-forget。真正的写盘由单消费者任务完成。
        """
        ...

    async def flush(self) -> None: ...

    async def query(
        self,
        session_id: str,
        *,
        types: Sequence[EventType] | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]: ...

    async def query_after(
        self,
        session_id: str,
        cursor: EventCursor,
        *,
        limit: int = 200,
    ) -> EventBatch: ...

    async def list_session_ids(self) -> list[str]: ...

    async def aclose(self) -> None: ...


class NullEventStore:
    """测试/最小配置用。"""

    def append_nowait(self, event: AgentEvent) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def query(
        self,
        session_id: str,
        *,
        types: Sequence[EventType] | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]:
        return []

    async def query_after(
        self,
        session_id: str,
        cursor: EventCursor,
        *,
        limit: int = 200,
    ) -> EventBatch:
        return EventBatch((), cursor)

    async def list_session_ids(self) -> list[str]:
        return []

    async def aclose(self) -> None:
        return None
