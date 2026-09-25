"""语义冲突检测与消解（记忆 V2 §25）。

本地 V1 用确定性策略：不引入 LLM 冲突判断，靠来源优先级 + 类型 + token 相关性。
重大约束冲突不让 LLM 自决，标记 REQUIRE_USER_CONFIRMATION（本期不自动写）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from codeagent.memory.dedup import token_jaccard
from codeagent.memory.judge import JudgeVerdict
from codeagent.memory.models import MemoryItem, MemorySource, MemoryType


class ConflictAction(StrEnum):
    KEEP_BOTH = "keep_both"
    SKIP_NEW = "skip_new"
    SUPERSEDE_OLD = "supersede_old"
    MERGE = "merge"
    REQUIRE_USER_CONFIRMATION = "require_user_confirmation"


@dataclass(frozen=True, slots=True)
class ConflictDecision:
    action: ConflictAction
    target_id: str | None = None
    reason: str = ""


class MemoryConflictResolver:
    def __init__(
        self,
        *,
        related_threshold: float = 0.2,
        low_confidence: float = 0.6,
    ) -> None:
        self._related_threshold = related_threshold
        self._low_confidence = low_confidence

    def resolve(
        self,
        *,
        content: str,
        source: MemorySource,
        verdict: JudgeVerdict,
        related: list[MemoryItem],
    ) -> ConflictDecision:
        conflict = self._most_related(content, verdict, related)
        if conflict is None:
            return ConflictDecision(ConflictAction.KEEP_BOTH, reason="no related memory")

        if source is MemorySource.USER_EXPLICIT:
            if verdict.type is MemoryType.CONSTRAINT and conflict.type is MemoryType.CONSTRAINT:
                # 约束级冲突：用户显式指令直接取代旧约束（§29 用户显式 > 旧记忆）。
                return ConflictDecision(
                    ConflictAction.SUPERSEDE_OLD, conflict.id, "user constraint overrides"
                )
            return ConflictDecision(
                ConflictAction.SUPERSEDE_OLD, conflict.id, "user-explicit update"
            )

        # 助手推导的低置信内容与已有记忆冲突 → 不写，避免污染。
        if verdict.confidence < self._low_confidence:
            return ConflictDecision(
                ConflictAction.SKIP_NEW, conflict.id, "low-confidence assistant conflict"
            )
        return ConflictDecision(ConflictAction.KEEP_BOTH, conflict.id, "kept alongside")

    def _most_related(
        self,
        content: str,
        verdict: JudgeVerdict,
        related: list[MemoryItem],
    ) -> MemoryItem | None:
        best: MemoryItem | None = None
        best_score = self._related_threshold
        for item in related:
            if item.type is not verdict.type:
                continue
            score = token_jaccard(content, item.content)
            if score >= best_score:
                best = item
                best_score = score
        return best
