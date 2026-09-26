from __future__ import annotations

import asyncio

import pytest

from codeagent.tool.resource_lock import ResourceLockManager


async def test_lock_evicted_after_release():
    mgr = ResourceLockManager()
    async with mgr.acquire(["a", "b"]):
        assert mgr.tracked_keys == 2
    # 释放后计数归零 → 锁表清空，不泄漏。
    assert mgr.tracked_keys == 0


async def test_concurrent_holders_keep_lock_and_serialize():
    mgr = ResourceLockManager()
    order: list[str] = []
    first_holds = asyncio.Event()
    release_first = asyncio.Event()

    async def first():
        async with mgr.acquire(["k"]):
            order.append("first-enter")
            first_holds.set()
            await release_first.wait()
            order.append("first-exit")

    async def second():
        await first_holds.wait()
        # first 持锁期间：key 仍被等待者引用，不能被驱逐。
        assert mgr.tracked_keys == 1
        async with mgr.acquire(["k"]):
            order.append("second-enter")

    t1 = asyncio.create_task(first())
    t2 = asyncio.create_task(second())
    await first_holds.wait()
    await asyncio.sleep(0)  # 让 second 进入等待
    release_first.set()
    await asyncio.gather(t1, t2)

    # 互斥成立：second 必须在 first 退出后才进入。
    assert order == ["first-enter", "first-exit", "second-enter"]
    assert mgr.tracked_keys == 0


async def test_cancel_while_waiting_does_not_leak():
    mgr = ResourceLockManager()
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder():
        async with mgr.acquire(["k"]):
            holding.set()
            await release.wait()

    async def waiter():
        async with mgr.acquire(["k"]):
            pass

    h = asyncio.create_task(holder())
    await holding.wait()
    w = asyncio.create_task(waiter())
    await asyncio.sleep(0)  # waiter 进入 await lock.acquire()
    w.cancel()
    with pytest.raises(asyncio.CancelledError):
        await w
    release.set()
    await h
    # 被取消的等待者不应留下悬空引用计数。
    assert mgr.tracked_keys == 0
