"""ContextBudgetPredictor：不能只看 currentTokens。

上下文文档 §11 的核心：150K < 167K 看起来安全，但下一轮加上 tool burst
和模型输出预留就已经 191K 了。所以触发条件是

    current >= softTrigger  OR  predicted >= hardTrigger

而不是单看当前占用比例。
"""

from __future__ import annotations

from dataclasses import dataclass

from codeagent.context.profile import ContextProfile


@dataclass(frozen=True, slots=True)
class ContextBudgetPrediction:
    current_tokens: int
    predicted_tokens: int
    soft_trigger: int
    hard_trigger: int
    context_window: int
    should_compact: bool
    over_hard_limit: bool
    reason: str

    @property
    def usage_ratio(self) -> float:
        return self.current_tokens / self.context_window if self.context_window else 0.0


class ContextBudgetPredictor:
    def predict(self, current_tokens: int, profile: ContextProfile) -> ContextBudgetPrediction:
        predicted = (
            current_tokens
            + profile.expected_tool_burst
            + profile.output_reserve
            + profile.safety_margin
        )
        soft = profile.soft_trigger
        hard = profile.hard_trigger

        over_soft = current_tokens >= soft
        over_predicted = predicted >= hard
        should_compact = over_soft or over_predicted

        if over_soft and over_predicted:
            reason = (
                f"current {current_tokens} >= soft {soft} "
                f"且 predicted {predicted} >= hard {hard}"
            )
        elif over_soft:
            reason = f"current {current_tokens} >= soft {soft}"
        elif over_predicted:
            reason = f"predicted {predicted} >= hard {hard}（下一轮风险）"
        else:
            reason = "safe"

        return ContextBudgetPrediction(
            current_tokens=current_tokens,
            predicted_tokens=predicted,
            soft_trigger=soft,
            hard_trigger=hard,
            context_window=profile.context_window,
            should_compact=should_compact,
            over_hard_limit=current_tokens >= hard,
            reason=reason,
        )
