from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Protocol, runtime_checkable

from codeagent.llm.message import Message
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec


class LlmErrorKind(StrEnum):
    RATE_LIMIT = "rate_limit"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    AUTH = "auth"
    INVALID_REQUEST = "invalid_request"
    CONTEXT_LIMIT = "context_limit"
    CONFIGURATION = "configuration"
    UNKNOWN = "unknown"


class LlmError(Exception):
    def __init__(self, message: str = "", *, kind: LlmErrorKind = LlmErrorKind.UNKNOWN) -> None:
        super().__init__(message)
        self.kind = kind


def provider_error_kind(error: Exception) -> LlmErrorKind:
    """只按结构化状态/异常类型分类，不从可能包含用户输入的异常文本猜测。"""
    if isinstance(error, LlmError):
        return error.kind
    if isinstance(error, TimeoutError) or type(error).__name__ == "APITimeoutError":
        return LlmErrorKind.TIMEOUT
    status = getattr(error, "status_code", None)
    if status == 429:
        return LlmErrorKind.RATE_LIMIT
    if status in (401, 403):
        return LlmErrorKind.AUTH
    if status in (408, 504):
        return LlmErrorKind.TIMEOUT
    if status in (500, 502, 503, 529) or type(error).__name__ == "APIConnectionError":
        return LlmErrorKind.UNAVAILABLE
    if status in (400, 404, 413, 422):
        return LlmErrorKind.INVALID_REQUEST
    return LlmErrorKind.UNKNOWN


@runtime_checkable
class LlmClient(Protocol):
    async def chat(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse: ...

    async def count_tokens(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        """精确计数。返回 None 表示该 Provider 不支持。

        这是网络调用，只允许在压缩决策边界和校准时用，不能进热路径。
        """
        ...


def effective_model_config(client: LlmClient, config: ModelConfig) -> ModelConfig:
    """Optional synchronous capability lookup; plain providers retain their call configuration."""
    resolve = getattr(client, 'effective_config', None)
    value = resolve(config) if callable(resolve) else config
    if not isinstance(value, ModelConfig):
        raise TypeError('effective_config must return ModelConfig')
    return value
