"""确定性 LLM 桩，供测试与离线跑通骨架用。

script 每一项可以是：
- str                     -> 一条纯文本回复（结束本轮）
- [(tool_name, args), ..] -> 一批 tool_use（可多个，用于并行 tool 协议测试）
- LlmResponse             -> 完全自定义
- callable(messages)      -> 动态决定
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeAlias

from codeagent.infra.ids import new_id, new_llm_call_id
from codeagent.llm.message import Block, Message, TextBlock, ToolUseBlock
from codeagent.llm.types import LlmResponse, ModelConfig, ToolSpec, Usage

ScriptedTurn: TypeAlias = (
    "str | Sequence[tuple[str, dict]] | LlmResponse | Callable[[Sequence[Message]], LlmResponse]"
)


class StubLlmClient:
    def __init__(self, script: Sequence[ScriptedTurn]) -> None:
        self._script = list(script)
        self._index = 0
        self.seen_calls: list[tuple[Message, ...]] = []

    @property
    def call_count(self) -> int:
        return self._index

    async def chat(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> LlmResponse:
        self.seen_calls.append(tuple(messages))
        if self._index >= len(self._script):
            # 脚本耗尽：给一条终止回复，避免测试悬挂。
            return LlmResponse(new_llm_call_id(), "stub: script exhausted")
        item = self._script[self._index]
        self._index += 1
        return self._materialize(item, messages)

    async def count_tokens(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        return None

    def _materialize(self, item: ScriptedTurn, messages: Sequence[Message]) -> LlmResponse:
        if isinstance(item, LlmResponse):
            return item
        if callable(item):
            return item(messages)
        if isinstance(item, str):
            return LlmResponse(
                new_llm_call_id(),
                item,
                blocks=(TextBlock(item),),
                stop_reason="end_turn",
                usage=Usage(input_tokens=len(str(messages)) // 4, output_tokens=len(item) // 4),
            )
        blocks: list[Block] = []
        for name, args in item:
            blocks.append(ToolUseBlock(id=new_id("tu"), name=name, arguments=dict(args)))
        return LlmResponse(
            new_llm_call_id(),
            "",
            blocks=tuple(blocks),
            stop_reason="tool_use",
            usage=Usage(),
        )
