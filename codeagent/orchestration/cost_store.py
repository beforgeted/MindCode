"""RunStore-backed call intents and cost facts, independent of asynchronous events."""
from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from codeagent.orchestration.run_store import SqliteRunStore

# Association only: counters and decisions always read durable RunStore facts.
_run: ContextVar[str | None] = ContextVar('cost_run', default=None)


@contextmanager
def cost_scope(run_id: str) -> Iterator[None]:
    token = _run.set(run_id)
    try:
        yield
    finally:
        _run.reset(token)


def cost_run() -> str | None:
    return _run.get()


class CostStore:
    def __init__(self, path: Path):
        self.store = SqliteRunStore(path)
        self._started = False
        self._start_lock = asyncio.Lock()

    async def _ready(self):
        async with self._start_lock:
            if not self._started:
                await self.store.start()
                self._started = True

    async def begin(self, call_id: str, run_id: str, provider: str, model: str) -> None:
        await self._ready()
        def op(db: sqlite3.Connection):
            db.execute('INSERT OR IGNORE INTO llm_cost_origin VALUES (?, 1)', (run_id,))
            db.execute('INSERT INTO llm_cost VALUES (?, ?, ?, ?, NULL, ?)',
                       (call_id, run_id, provider, model, 'pending'))
        await self.store._run(op)

    async def initialize_run(self, run_id: str, *, resumed: bool) -> None:
        await self._ready()
        def op(db: sqlite3.Connection):
            # An old run has no trustworthy prior billing coverage. Never seed it as free.
            db.execute('INSERT OR IGNORE INTO llm_cost_origin VALUES (?, ?)',
                       (run_id, 0 if resumed else 1))
        await self.store._run(op)

    async def finish(self, call_id: str, charge: int | None, status: str) -> None:
        await self._ready()
        def op(db: sqlite3.Connection):
            changed = db.execute('UPDATE llm_cost SET pico_usd=?, status=? WHERE call_id=?',
                                 (str(charge) if charge is not None else None,
                                  status, call_id)).rowcount
            if changed != 1:
                raise ValueError('cost intent is missing')
        await self.store._run(op)

    async def total(self, run_id: str) -> tuple[int, int]:
        await self._ready()
        def op(db: sqlite3.Connection):
            rows = db.execute('SELECT pico_usd FROM llm_cost WHERE master_run_id=?',
                              (run_id,)).fetchall()
            origin = db.execute('SELECT complete FROM llm_cost_origin WHERE master_run_id=?',
                                (run_id,)).fetchone()
            return (sum(int(row[0]) for row in rows if row[0] is not None),
                    sum(row[0] is None for row in rows) + int(origin is not None and not origin[0]))
        return await self.store._run(op)

