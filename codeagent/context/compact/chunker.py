from __future__ import annotations

from dataclasses import dataclass

from codeagent.context.history.turn import ConversationTurn
from codeagent.context.token_estimator import TokenEstimator
from codeagent.llm.message import Message


@dataclass(frozen=True, slots=True)
class CompactionChunk:
    turns: tuple[ConversationTurn, ...]
    messages: tuple[Message, ...]
    tokens: int
    oversized: bool = False


class HistoryChunker:
    """按完整 turn 分块；任何情况下都不拆 tool exchange。"""

    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def chunk(
        self,
        turns: list[ConversationTurn],
        *,
        max_tokens: int,
    ) -> tuple[CompactionChunk, ...]:
        if max_tokens <= 0:
            raise ValueError("max_tokens 必须大于 0")
        chunks: list[CompactionChunk] = []
        current: list[ConversationTurn] = []
        current_tokens = 0
        for turn in turns:
            turn_tokens = self._estimator.estimate(turn.messages)
            if current and current_tokens + turn_tokens > max_tokens:
                chunks.append(self._build(current, current_tokens, max_tokens))
                current = []
                current_tokens = 0
            current.append(turn)
            current_tokens += turn_tokens
            if turn_tokens > max_tokens:
                chunks.append(self._build(current, current_tokens, max_tokens))
                current = []
                current_tokens = 0
        if current:
            chunks.append(self._build(current, current_tokens, max_tokens))
        return tuple(chunks)

    @staticmethod
    def _build(
        turns: list[ConversationTurn], tokens: int, max_tokens: int
    ) -> CompactionChunk:
        return CompactionChunk(
            turns=tuple(turns),
            messages=tuple(message for turn in turns for message in turn.messages),
            tokens=tokens,
            oversized=tokens > max_tokens,
        )
