from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from codeagent.llm.message import Message
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec


class LlmError(Exception):
    pass


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
