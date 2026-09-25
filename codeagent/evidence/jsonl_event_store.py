"""JSONL RawEventStore。

Python 特有的实现决策：并发追加**不加锁**，而是
`asyncio.Queue` + 单消费者任务 + 批量 flush。

理由：
- 顺序天然有保证（单写者），多个并发 AgentRun 不会互相插行；
- 可以批量 fsync，比每条一次 flush 便宜得多；
- `append_nowait()` 保持同步，让 ConversationHistory.append() 不必是 async。

退出前必须 drain（记忆 V2 §44：避免丢掉最后一轮候选 Memory）。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from codeagent.evidence.cursor import EventBatch, EventCursor, SequencedEvent
from codeagent.evidence.models import AgentEvent, EventType

_SENTINEL = object()


class JsonlEventStore:
    def __init__(
        self,
        root: Path,
        *,
        flush_interval_seconds: float = 1.0,
        batch_size: int = 64,
    ) -> None:
        self._root = Path(root)
        self._sessions_dir = self._root / "sessions"
        self._sessions_dir.mkdir(parents=True, exist_ok=True)
        self._queue: asyncio.Queue = asyncio.Queue()
        self._writer: asyncio.Task | None = None
        self._flush_interval = flush_interval_seconds
        self._batch_size = batch_size
        self._closed = False
        self.dropped = 0

    # --- 写入 ---

    def append_nowait(self, event: AgentEvent) -> None:
        if self._closed:
            self.dropped += 1
            return
        self._queue.put_nowait(event)
        self._ensure_writer()

    def _ensure_writer(self) -> None:
        if self._writer is not None and not self._writer.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 没有事件循环：先攒着，start()/aclose() 时再写
        self._writer = loop.create_task(self._run(), name="jsonl-event-writer")

    async def start(self) -> None:
        self._ensure_writer()

    async def flush(self) -> None:
        """等队列排空。仅用于测试与退出，不要放进热路径。"""
        self._ensure_writer()
        await self._queue.join()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._ensure_writer()
        await self._queue.join()
        self._closed = True
        if self._writer is not None:
            self._queue.put_nowait(_SENTINEL)
            await self._writer
            self._writer = None
        # 兜底：无事件循环期间攒下的残留
        leftovers = self._drain_sync()
        if leftovers:
            await asyncio.to_thread(self._write_batch, leftovers)

    async def _run(self) -> None:
        batch: list[AgentEvent] = []
        while True:
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=self._flush_interval)
            except TimeoutError:
                if batch:
                    await asyncio.to_thread(self._write_batch, batch)
                    for _ in batch:
                        self._queue.task_done()
                    batch = []
                continue
            if item is _SENTINEL:
                self._queue.task_done()
                break
            batch.append(item)
            if len(batch) >= self._batch_size:
                await asyncio.to_thread(self._write_batch, batch)
                for _ in batch:
                    self._queue.task_done()
                batch = []
            elif self._queue.empty():
                await asyncio.to_thread(self._write_batch, batch)
                for _ in batch:
                    self._queue.task_done()
                batch = []
        if batch:
            await asyncio.to_thread(self._write_batch, batch)
            for _ in batch:
                self._queue.task_done()

    def _drain_sync(self) -> list[AgentEvent]:
        out: list[AgentEvent] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item is not _SENTINEL:
                out.append(item)
            self._queue.task_done()
        return out

    def _write_batch(self, batch: Sequence[AgentEvent]) -> None:
        by_session: dict[str, list[AgentEvent]] = {}
        for event in batch:
            by_session.setdefault(event.session_id, []).append(event)
        for session_id, events in by_session.items():
            path = self._path_for(session_id)
            with path.open("a", encoding="utf-8") as handle:
                for event in events:
                    handle.write(json.dumps(event.to_json_dict(), ensure_ascii=False) + "\n")

    def _path_for(self, session_id: str) -> Path:
        return self._sessions_dir / f"{session_id}.jsonl"

    # --- 读取 ---

    async def query(
        self,
        session_id: str,
        *,
        types: Sequence[EventType] | None = None,
        limit: int | None = None,
    ) -> list[AgentEvent]:
        await self.flush()
        return await asyncio.to_thread(self._query_sync, session_id, types, limit)

    def _query_sync(
        self,
        session_id: str,
        types: Sequence[EventType] | None,
        limit: int | None,
    ) -> list[AgentEvent]:
        path = self._path_for(session_id)
        if not path.exists():
            return []
        wanted = {str(t) for t in types} if types else None
        out: list[AgentEvent] = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if wanted is not None and raw["type"] not in wanted:
                    continue
                out.append(_from_json(raw))
                if limit is not None and len(out) >= limit:
                    break
        return out

    async def query_after(
        self,
        session_id: str,
        cursor: EventCursor,
        *,
        limit: int = 200,
    ) -> EventBatch:
        await self.flush()
        return await asyncio.to_thread(
            self._query_after_sync,
            session_id,
            cursor,
            max(1, limit),
        )

    def _query_after_sync(
        self,
        session_id: str,
        cursor: EventCursor,
        limit: int,
    ) -> EventBatch:
        path = self._path_for(session_id)
        if not path.exists():
            return EventBatch((), cursor)
        events: list[SequencedEvent] = []
        next_ordinal = cursor.next_ordinal
        with path.open("r", encoding="utf-8") as handle:
            for ordinal, line in enumerate(handle):
                if ordinal < cursor.next_ordinal:
                    continue
                raw = json.loads(line)
                events.append(SequencedEvent(ordinal, _from_json(raw)))
                next_ordinal = ordinal + 1
                if len(events) >= limit:
                    break
        return EventBatch(tuple(events), EventCursor(next_ordinal))

    async def list_session_ids(self) -> list[str]:
        await self.flush()
        return await asyncio.to_thread(self._list_session_ids_sync)

    def _list_session_ids_sync(self) -> list[str]:
        return sorted(path.stem for path in self._sessions_dir.glob("*.jsonl") if path.is_file())


def _from_json(raw: dict) -> AgentEvent:
    from datetime import datetime

    return AgentEvent(
        type=EventType(raw["type"]),
        session_id=raw["session_id"],
        event_id=raw["event_id"],
        agent_run_id=raw.get("agent_run_id"),
        tool_run_id=raw.get("tool_run_id"),
        turn_id=raw.get("turn_id"),
        created_at=datetime.fromisoformat(raw["created_at"]),
        payload=raw.get("payload") or {},
    )
