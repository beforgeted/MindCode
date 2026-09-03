"""ContextManager：LLM 调用前的唯一入口。

上下文文档把"ContextManager 收口"排到 Phase 3，意味着 P1/P2 先把裁剪和压缩
写进 Agent 主循环、再搬出来 —— 那是纯返工。所以这个外壳 P1 就建，
组件逐期往里填，Agent 主循环从第一天起就不知道任何压缩策略。

流水线（上下文文档 §8 / 记忆 V2 §11）：

    ① 裁历史图片
    ② 降级历史 tool 结果
    ③ 估算当前占用
    ④ 预测下一轮风险
    ⑤ 需要则压缩旧 turn        <- P2
    ⑥ 加载相关长期 Memory      <- P3
    ⑦ 分配 Memory token 预算   <- P3
    ⑧ 组装最终上下文
    ⑨ 校验 hard limit

顺序很重要：先 Prune/Offload/Compact，再 Memory Retrieval。否则 Context
已经很满时还盲目注入长期 Memory，只会加压。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.context.budget import ContextBudgetPrediction, ContextBudgetPredictor
from codeagent.context.compact.base import CompactionResult, HistoryCompactor, NullCompactor
from codeagent.context.history.conversation_history import (
    ConversationHistory,
    validate_tool_protocol,
)
from codeagent.context.history.turn import TurnIdPartitioner, TurnPartitioner
from codeagent.context.profile import ContextProfile
from codeagent.context.prune.image_pruner import ImagePayloadPruner
from codeagent.context.prune.tool_result_offloader import ToolResultOffloader
from codeagent.context.token_estimator import HeuristicTokenEstimator, TokenEstimator
from codeagent.infra import metrics as M
from codeagent.infra.metrics import Metrics
from codeagent.llm.message import ContextCategory, Message


class ContextOverflowError(RuntimeError):
    """压缩后仍超 hard limit。

    这里抛异常而不是 `messages[-N:]` 强删是刻意的：静默有损删除会让
    Agent 在用户看不见的地方丢掉关键约束和决策。
    """


@dataclass(frozen=True, slots=True)
class ContextPreparationResult:
    messages: tuple[Message, ...]
    prediction: ContextBudgetPrediction
    breakdown: dict[ContextCategory, int] = field(default_factory=dict)
    compaction: CompactionResult | None = None
    tokens_before: int = 0
    tokens_after_prune: int = 0
    tokens_final: int = 0
    image_tokens_removed: int = 0
    tool_tokens_removed: int = 0

    @property
    def compacted(self) -> bool:
        return self.compaction is not None and self.compaction.compacted


class ContextManager:
    def __init__(
        self,
        *,
        estimator: TokenEstimator | None = None,
        predictor: ContextBudgetPredictor | None = None,
        partitioner: TurnPartitioner | None = None,
        image_pruner: ImagePayloadPruner | None = None,
        tool_offloader: ToolResultOffloader | None = None,
        compactor: HistoryCompactor | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.estimator = estimator or HeuristicTokenEstimator()
        self.predictor = predictor or ContextBudgetPredictor()
        self.partitioner = partitioner or TurnIdPartitioner()
        self.image_pruner = image_pruner or ImagePayloadPruner(self.estimator)
        self.tool_offloader = tool_offloader or ToolResultOffloader(self.estimator)
        self.compactor = compactor or NullCompactor()
        self.metrics = metrics or Metrics()

    async def prepare(
        self,
        history: ConversationHistory,
        profile: ContextProfile,
        *,
        force_compact: bool = False,
        focus: str | None = None,
    ) -> ContextPreparationResult:
        with self.metrics.timer(M.CONTEXT_PREPARE_MS):
            return await self._prepare(history, profile, force_compact=force_compact, focus=focus)

    async def _prepare(
        self,
        history: ConversationHistory,
        profile: ContextProfile,
        *,
        force_compact: bool,
        focus: str | None,
    ) -> ContextPreparationResult:
        messages = history.snapshot()
        tokens_before = self.estimator.estimate(messages)
        self.metrics.gauge(M.CONTEXT_TOKENS_BEFORE, tokens_before)

        turns = self.partitioner.partition(messages)
        turn_order = [turn.turn_id for turn in turns]
        hot_turns = frozenset(turn_order[-profile.image_payload_hot_turns :])

        # ① 图片
        image_outcome = self.image_pruner.prune(messages, hot_turn_ids=hot_turns)
        messages = list(image_outcome.messages)

        # ② tool 结果
        tool_outcome = self.tool_offloader.offload(
            messages, profile=profile, turn_order=turn_order
        )
        messages = list(tool_outcome.messages)

        tokens_after_prune = self.estimator.estimate(messages)
        self.metrics.gauge(M.CONTEXT_TOKENS_AFTER_PRUNE, tokens_after_prune)
        self.metrics.incr(M.CONTEXT_IMAGE_TOKENS_REMOVED, image_outcome.tokens_removed)
        self.metrics.incr(M.CONTEXT_TOOL_TOKENS_REMOVED, tool_outcome.tokens_removed)

        # 裁剪结果写回 History：裁掉的 payload 不应该在下一轮又被重新估算。
        if image_outcome.items_pruned or tool_outcome.items_offloaded:
            history.replace_messages(messages)

        # ③④ 预算与风险预测
        prediction = self.predictor.predict(tokens_after_prune, profile)

        # ⑤ 压缩
        compaction: CompactionResult | None = None
        if force_compact or prediction.should_compact:
            compaction = await self.compactor.compact(messages, profile=profile, focus=focus)
            if compaction.compacted:
                messages = list(compaction.messages)
                history.replace_messages(messages)
                history.compaction_count += 1
                self.metrics.incr(M.CONTEXT_COMPACTION_COUNT)
            else:
                self.metrics.incr(M.CONTEXT_COMPACTION_SKIPPED)

        # ⑥⑦ Memory 注入 —— P3 挂载点，放在 prune/compact 之后。

        tokens_final = self.estimator.estimate(messages)
        self.metrics.gauge(M.CONTEXT_TOKENS_AFTER_COMPACT, tokens_final)

        # ⑨ hard limit 校验
        if tokens_final > profile.hard_trigger:
            raise ContextOverflowError(
                f"上下文 {tokens_final} tokens 超过 hard limit {profile.hard_trigger}"
                f"（window {profile.context_window}）。"
                f"压缩状态: {compaction.reason if compaction else '未触发'}。"
                "P1 阶段没有 HistoryCompactor，这里刻意抛错而不是静默截断历史。"
            )

        validate_tool_protocol(messages)

        return ContextPreparationResult(
            messages=tuple(messages),
            prediction=prediction,
            breakdown=self.breakdown(messages),
            compaction=compaction,
            tokens_before=tokens_before,
            tokens_after_prune=tokens_after_prune,
            tokens_final=tokens_final,
            image_tokens_removed=image_outcome.tokens_removed,
            tool_tokens_removed=tool_outcome.tokens_removed,
        )

    def breakdown(self, messages: list[Message]) -> dict[ContextCategory, int]:
        """`/context` 的分项占用。靠 Message.category 归因，所以那个字段是 P0 就要有的。"""
        out: dict[ContextCategory, int] = {}
        for message in messages:
            tokens = self.estimator.estimate_message(message)
            out[message.category] = out.get(message.category, 0) + tokens
        return out
