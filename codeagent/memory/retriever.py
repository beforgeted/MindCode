from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from codeagent.context.compact.models import TaskCheckpoint
from codeagent.memory.models import MemoryItem, MemorySearchQuery, MemorySource
from codeagent.memory.repository import MemoryRepository

_CJK_RE = re.compile(r"[　-ヿ㐀-鿿豈-﫿]+")
_LATIN_RE = re.compile(r"[A-Za-z0-9_.-]{2,32}")


@dataclass(frozen=True, slots=True)
class RankedMemory:
    item: MemoryItem
    score: float


@runtime_checkable
class MemoryRetriever(Protocol):
    async def retrieve(
        self,
        query: str,
        *,
        checkpoint: TaskCheckpoint | None = None,
        limit: int = 50,
    ) -> Sequence[RankedMemory]: ...


class NullMemoryRetriever:
    async def retrieve(
        self,
        query: str,
        *,
        checkpoint: TaskCheckpoint | None = None,
        limit: int = 50,
    ) -> Sequence[RankedMemory]:
        return ()


class KeywordMemoryRetriever:
    def __init__(
        self,
        repository: MemoryRepository,
        project_id: str,
        *,
        source_weights: dict[MemorySource, float] | None = None,
        importance_weight: float = 0.03,
    ) -> None:
        self._repository = repository
        self._project_id = project_id
        self._source_weights = source_weights or {}
        self._importance_weight = importance_weight

    async def retrieve(
        self,
        query: str,
        *,
        checkpoint: TaskCheckpoint | None = None,
        limit: int = 50,
    ) -> Sequence[RankedMemory]:
        user_terms = _terms(query, 12)
        checkpoint_terms = _checkpoint_terms(checkpoint, 8)
        scores: dict[str, float] = {}
        items: dict[str, MemoryItem] = {}
        weighted_terms = [
            *((term, 2.0) for term in user_terms),
            *((term, 0.5) for term in checkpoint_terms),
        ]
        for term, weight in weighted_terms:
            hits = await self._repository.search(
                MemorySearchQuery(self._project_id, term, limit=10)
            )
            for rank, hit in enumerate(hits, 1):
                items[hit.item.id] = hit.item
                scores[hit.item.id] = scores.get(hit.item.id, 0.0) + weight / rank
        ranked = [
            RankedMemory(item, self._rerank(scores[memory_id], item))
            for memory_id, item in items.items()
        ]
        ranked.sort(
            key=lambda value: (
                -value.score,
                -value.item.updated_at.timestamp(),
                value.item.id,
            )
        )
        return tuple(ranked[: max(0, limit)])

    def _rerank(self, base: float, item: MemoryItem) -> float:
        # §29 优先级阶梯：助手推导记忆权重低于用户显式 / 已验证记忆。
        weight = self._source_weights.get(item.source, 1.0)
        importance = (item.importance or 0) * self._importance_weight
        return base * weight + importance


def _checkpoint_terms(checkpoint: TaskCheckpoint | None, limit: int) -> tuple[str, ...]:
    if checkpoint is None:
        return ()
    text = " ".join(
        filter(
            None,
            [
                checkpoint.goal or "",
                *checkpoint.constraints,
                *(decision.decision for decision in checkpoint.decisions),
                *checkpoint.open_issues,
                *checkpoint.next_steps,
            ],
        )
    )
    return _terms(text, limit)


def _terms(text: str, limit: int) -> tuple[str, ...]:
    normalized = unicodedata.normalize("NFKC", text)
    out: list[str] = []
    for match in _LATIN_RE.finditer(normalized):
        _append(out, match.group(0).casefold(), limit)
    for match in _CJK_RE.finditer(normalized):
        value = match.group(0)
        if len(value) == 2:
            _append(out, value, limit)
        elif len(value) >= 3:
            for index in range(len(value) - 2):
                _append(out, value[index : index + 3], limit)
    return tuple(out[:limit])


def _append(items: list[str], value: str, limit: int) -> None:
    if len(items) < limit and value not in items:
        items.append(value)
