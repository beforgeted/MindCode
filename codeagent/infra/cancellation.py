"""取消机制。

决策：`asyncio.CancelledError` 是**主**机制，它在每个 await 点自动传播。
CancellationToken 只用于两处 CancelledError 覆盖不到的地方：

1. 传给 subprocess，让工具能主动 kill 子进程；
2. 少数没有 await 点的长循环里做协作式检查。

不要建第二套并行的取消宇宙。另外注意：捕获工具异常时必须用
`except Exception`，绝不能用 `except BaseException` 或裸 `except`，
否则会吞掉 CancelledError（3.8+ 起它继承自 BaseException）。
"""

from __future__ import annotations

import asyncio


class CancelledByUser(Exception):
    """协作式取消信号。与 asyncio.CancelledError 区分，便于区别"外部取消"和"用户取消"。"""


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledByUser("run cancelled")

    async def wait(self) -> None:
        await self._event.wait()

    def child(self) -> CancellationToken:
        """派生子 Token：父取消则子取消，子取消不影响父。

        取消链：MasterRun -> AgentRun -> ToolRun。
        """
        child = CancellationToken()

        async def _propagate() -> None:
            await self._event.wait()
            child.cancel()

        task = asyncio.create_task(_propagate())
        # 防止 task 被 GC；父 Token 生命周期覆盖子 Token。
        child._parent_task = task  # type: ignore[attr-defined]
        return child
