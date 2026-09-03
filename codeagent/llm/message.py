"""Message：LLM 协议类型。

两个 P0 决策落在这里：

1. `category` 标签 —— 没有它 `/context` 的分项占用根本算不出来。
2. `Role.INTERNAL_CONTEXT` —— 压缩后注入的 TaskCheckpoint 不是用户的新请求，
   也不该伪造一条 assistant 的"好的我已了解"。它是系统维护的历史状态，
   由 Provider 适配层决定怎么映射（Anthropic 只有 user/assistant，
   映射成带 <internal_context> 标签的 user 消息）。

不变式：`blocks` 永不原地修改。裁剪器一律用 `with_blocks()` 产生新 Message，
因此 `token_estimate` 缓存永远有效。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, TypeAlias


class Role(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    INTERNAL_CONTEXT = "internal_context"


class ContextCategory(StrEnum):
    """token 归因用。`/context` 的 breakdown 直接按它聚合。"""

    SYSTEM = "system"
    CHECKPOINT = "checkpoint"
    MEMORY = "memory"
    CONVERSATION = "conversation"
    TOOL_RESULT = "tool_result"
    IMAGE = "image"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class TextBlock:
    text: str
    kind: Literal["text"] = "text"


@dataclass(frozen=True, slots=True)
class ImageBlock:
    media_type: str
    data: str | None = None
    summary: str | None = None
    kind: Literal["image"] = "image"

    @property
    def pruned(self) -> bool:
        return self.data is None


@dataclass(frozen=True, slots=True)
class ToolUseBlock:
    id: str
    name: str
    arguments: dict
    kind: Literal["tool_use"] = "tool_use"


@dataclass(frozen=True, slots=True)
class ToolResultBlock:
    tool_use_id: str
    content: str
    is_error: bool = False
    artifact_uri: str | None = None
    truncated: bool = False
    kind: Literal["tool_result"] = "tool_result"


Block: TypeAlias = TextBlock | ImageBlock | ToolUseBlock | ToolResultBlock


@dataclass
class Message:
    role: Role
    blocks: tuple[Block, ...]
    category: ContextCategory = ContextCategory.CONVERSATION
    turn_id: str | None = None
    event_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # 估算缓存。历史每轮全量重估是 O(n^2)，必须缓存。
    token_estimate: int | None = None

    def with_blocks(self, blocks: Sequence[Block]) -> Message:
        return Message(
            role=self.role,
            blocks=tuple(blocks),
            category=self.category,
            turn_id=self.turn_id,
            event_id=self.event_id,
            created_at=self.created_at,
            token_estimate=None,
        )

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if isinstance(b, TextBlock))

    @property
    def tool_uses(self) -> tuple[ToolUseBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolUseBlock))

    @property
    def tool_results(self) -> tuple[ToolResultBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ToolResultBlock))

    @property
    def images(self) -> tuple[ImageBlock, ...]:
        return tuple(b for b in self.blocks if isinstance(b, ImageBlock))

    # --- 构造快捷方式 ---

    @staticmethod
    def system(text: str) -> Message:
        return Message(Role.SYSTEM, (TextBlock(text),), ContextCategory.SYSTEM)

    @staticmethod
    def user(text: str, *, turn_id: str | None = None) -> Message:
        return Message(Role.USER, (TextBlock(text),), ContextCategory.CONVERSATION, turn_id)

    @staticmethod
    def assistant(blocks: Sequence[Block], *, turn_id: str | None = None) -> Message:
        return Message(Role.ASSISTANT, tuple(blocks), ContextCategory.CONVERSATION, turn_id)

    @staticmethod
    def tool(blocks: Sequence[ToolResultBlock], *, turn_id: str | None = None) -> Message:
        """一个 assistant turn 的**全部** tool_result 必须放进同一条消息。

        这条约束是 tool 协议不变式的基础，见 tests/test_tool_protocol.py。
        """
        return Message(Role.TOOL, tuple(blocks), ContextCategory.TOOL_RESULT, turn_id)

    @staticmethod
    def internal_context(text: str, category: ContextCategory) -> Message:
        return Message(Role.INTERNAL_CONTEXT, (TextBlock(text),), category)
