"""Budget complete requests with the system's existing token estimator."""
from __future__ import annotations

from collections.abc import Sequence

from codeagent.context.token_estimator import TokenEstimator, estimate_request
from codeagent.llm.message import Message
from codeagent.llm.types import LlmResponse, ModelConfig


class RequestBudgetError(ValueError):
    pass


def check_request(
    messages: Sequence[Message], config: ModelConfig, estimator: TokenEstimator,
    *, repair_prompt: str = "",
) -> int:
    request = (*messages, Message.user(repair_prompt)) if repair_prompt else messages
    estimated = estimate_request(estimator, request, config)
    if (config.max_output_tokens <= 0
            or estimated + config.max_output_tokens >= config.context_window):
        raise RequestBudgetError(
            f"request budget exceeded: input={estimated}, output={config.max_output_tokens}, "
            f"window={config.context_window}"
        )
    return estimated


def check_response(response: LlmResponse) -> None:
    if response.stop_reason not in (None, "end_turn", "stop", "stop_sequence"):
        raise ValueError(f"incomplete model response: {response.stop_reason}")
    if not response.content.strip() or response.has_tool_uses:
        raise ValueError("expected a complete text response")
