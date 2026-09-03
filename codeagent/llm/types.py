from __future__ import annotations

from dataclasses import dataclass, field

from codeagent.llm.message import Block, ToolUseBlock


@dataclass(frozen=True, slots=True)
class ModelConfig:
    model: str = "claude-sonnet-5"
    max_output_tokens: int = 8192
    temperature: float = 1.0
    context_window: int = 200_000
    # 压缩的 Map 阶段是纯结构化抽取，用便宜快的模型；
    # Reduce 要判断状态冲突和取舍，用主模型。见 P2。
    map_model: str = "claude-haiku-4-5-20251001"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """给 LLM 的工具声明。"""

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True, slots=True)
class LlmResponse:
    llm_call_id: str
    content: str
    blocks: tuple[Block, ...] = ()
    stop_reason: str | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))

    @property
    def has_tool_uses(self) -> bool:
        return bool(self.tool_uses)
