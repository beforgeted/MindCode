"""按资源 key 加锁。

两条纪律（Python 多 Agent 文档 §20 自己也提了"真正实现时需要考虑"）：

1. 一批 tool 需要多把锁时，**锁 key 全局排序后按序获取**，否则必然死锁；
2. 持锁期间禁止调 LLM。

锁表清理（P6）：`dict[str, Lock]` 在长跑进程下只增不减，是内存泄漏。用**引用计数**
驱逐——每次 acquire 前对 key `+1`（必须在 `await lock.acquire()` 之前，这样并发等待者
能让锁在 event-loop 切换点存活，不被误删），释放后 `-1`；计数归零且锁未被持有时删除。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager


class ResourceLockManager:
    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}
        self._refs: dict[str, int] = {}

    def _reserve(self, key: str) -> asyncio.Lock:
        """登记一个即将获取的等待者，并返回该 key 的锁（必要时新建）。"""
        self._refs[key] = self._refs.get(key, 0) + 1
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def _unreserve(self, key: str) -> None:
        """注销一个等待者，计数归零且锁空闲时驱逐，避免锁表无限增长。"""
        count = self._refs.get(key, 0) - 1
        if count <= 0:
            self._refs.pop(key, None)
            lock = self._locks.get(key)
            if lock is not None and not lock.locked():
                del self._locks[key]
        else:
            self._refs[key] = count

    @asynccontextmanager
    async def acquire(self, keys: Sequence[str]) -> AsyncIterator[None]:
        ordered = sorted(set(keys))
        reserved: list[str] = []
        acquired: list[tuple[str, asyncio.Lock]] = []
        try:
            for key in ordered:
                lock = self._reserve(key)
                reserved.append(key)
                await lock.acquire()  # 取消/异常在此抛出：finally 用 reserved 回退计数
                acquired.append((key, lock))
            yield
        finally:
            for _key, lock in reversed(acquired):
                lock.release()
            for key in reversed(reserved):
                self._unreserve(key)

    @property
    def tracked_keys(self) -> int:
        return len(self._locks)

