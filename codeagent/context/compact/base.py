"""HistoryCompactor 接口 —— P2 的挂载点。

P1 只装一个 NullCompactor：它诚实地报告"没压"，于是 ContextManager 在越过
hard limit 时会抛 ContextOverflowError。这是**故意**的：

    宁可暂时保留更多 Context 甚至直接失败，
    也不能为了压缩而静默丢失关键状态。（上下文文档 §28）

P2 落地时在这里补 TurnPartitioner -> HistoryChunker -> HistoryMapSummarizer
-> TaskStateReducer -> TaskCheckpoint 的完整链路，ContextManager 一行不改。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from codeagent.context.profile import ContextProfile
from codeagent.llm.message import Message

if TYPE_CHECKING:
    from codeagent.context.compact.models import TaskCheckpoint
    from codeagent.context.history.turn import TurnStatus


@dataclass(frozen=True, slots=True)
class CompactionResult:
    compacted: bool
    messages: tuple[Message, ...]
    checkpoint: TaskCheckpoint | None = None
    tokens_before: int = 0
    tokens_after: int = 0
    map_chunks: int = 0
    map_failures: int = 0
    reason: str = ""

    @property
    def tokens_released(self) -> int:
        return max(0, self.tokens_before - self.tokens_after)


@runtime_checkable
class HistoryCompactor(Protocol):
    async def compact(
        self,
        messages: Sequence[Message],
        *,
        profile: ContextProfile,
        focus: str | None = None,
        checkpoint: TaskCheckpoint | None = None,
        turn_statuses: dict[str, TurnStatus] | None = None,
    ) -> CompactionResult: ...


class NullCompactor:
    """P1 占位。"""

    async def compact(
        self,
        messages: Sequence[Message],
        *,
        profile: ContextProfile,
        focus: str | None = None,
        checkpoint: TaskCheckpoint | None = None,
        turn_statuses: dict[str, TurnStatus] | None = None,
    ) -> CompactionResult:
        return CompactionResult(
            compacted=False,
            messages=tuple(messages),
            reason="P2 未实现：HistoryCompactor 尚未接入，本轮不压缩",
        )
