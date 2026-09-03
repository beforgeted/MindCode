"""按资源 key 加锁。

两条纪律（Python 多 Agent 文档 §20 自己也提了"真正实现时需要考虑"）：

1. 一批 tool 需要多把锁时，**锁 key 全局排序后按序获取**，否则必然死锁；
2. 持锁期间禁止调 LLM。

锁表清理留到 P6（长跑进程下 `dict[str, Lock]` 会无限增长）。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager


class ResourceLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire(self, keys: Sequence[str]) -> AsyncIterator[None]:
        ordered = sorted(set(keys))
        acquired: list[asyncio.Lock] = []
        try:
            for key in ordered:
                lock = self._lock(key)
                await lock.acquire()
                acquired.append(lock)
            yield
        finally:
            for lock in reversed(acquired):
                lock.release()

    @property
    def tracked_keys(self) -> int:
        return len(self._locks)
