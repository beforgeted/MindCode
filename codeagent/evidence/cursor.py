from __future__ import annotations

from dataclasses import dataclass

from codeagent.evidence.models import AgentEvent


@dataclass(frozen=True, slots=True)
class EventCursor:
    next_ordinal: int = 0

    def __post_init__(self) -> None:
        if self.next_ordinal < 0:
            raise ValueError("Event cursor 不能为负数")


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    ordinal: int
    event: AgentEvent


@dataclass(frozen=True, slots=True)
class EventBatch:
    events: tuple[SequencedEvent, ...]
    next_cursor: EventCursor
