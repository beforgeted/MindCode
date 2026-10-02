"""Budget the actual serialized compaction request with the existing estimator."""
from __future__ import annotations

from collections.abc import Sequence

from codeagent.context.compact.models import CompactionPayloadError
from codeagent.context.token_estimator import TokenEstimator, estimate_request
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig

REPAIR_PROMPT = (
    'The prior output was invalid. Return one strict JSON object only; '
    'no prose or extra keys.'
)


class CompactionBudgetError(CompactionPayloadError):
    pass


def request_fits(
    messages: Sequence[Message], config: ModelConfig, estimator: TokenEstimator, *,
    reserve_repair: bool = True,
) -> bool:
    request = (*messages, Message.user(REPAIR_PROMPT)) if reserve_repair else messages
    return (config.max_output_tokens > 0
            and estimate_request(estimator, request, config) + config.max_output_tokens
            < config.context_window)
