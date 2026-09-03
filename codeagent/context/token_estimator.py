"""TokenEstimator：全系统唯一的 token 估算入口。

P0 决策（上下文文档 §9）：不允许存在第二套估算规则。任何需要 token 数的地方
都走这个接口。

Python 特有的双轨设计：
- 热路径用启发式，结果缓存在 Message 上（历史每轮全量重估是 O(n^2)）；
- 精确计数（Anthropic 的 count_tokens）是**网络调用**，只在压缩决策边界
  和校准时用。CalibratedTokenEstimator 用精确值反过来修正启发式的系数。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from codeagent.llm.client import LlmClient
from codeagent.llm.message import ImageBlock, Message, TextBlock, ToolResultBlock, ToolUseBlock
from codeagent.llm.types import ModelConfig, ToolSpec

# 中文≈1 token/字，英文/代码≈3.6 字符/token。
_CJK_TOKENS_PER_CHAR = 1.0
_LATIN_CHARS_PER_TOKEN = 3.6
_MESSAGE_OVERHEAD = 8
_TOOL_USE_OVERHEAD = 16
# 没有图片尺寸时的保守估计（Anthropic 约 (w*h)/750）。
_IMAGE_FALLBACK_TOKENS = 1600


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (
        0x3000 <= code <= 0x303F
        or 0x3040 <= code <= 0x30FF
        or 0x3400 <= code <= 0x4DBF
        or 0x4E00 <= code <= 0x9FFF
        or 0xF900 <= code <= 0xFAFF
        or 0xFF00 <= code <= 0xFFEF
    )


def estimate_text(text: str) -> int:
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    latin = len(text) - cjk
    return int(cjk * _CJK_TOKENS_PER_CHAR + latin / _LATIN_CHARS_PER_TOKEN) + 1


@runtime_checkable
class TokenEstimator(Protocol):
    def estimate_message(self, message: Message) -> int: ...

    def estimate(self, messages: Sequence[Message]) -> int: ...

    async def count_exact(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None: ...


class HeuristicTokenEstimator:
    def estimate_message(self, message: Message) -> int:
        if message.token_estimate is not None:
            return message.token_estimate
        total = _MESSAGE_OVERHEAD
        for block in message.blocks:
            if isinstance(block, TextBlock):
                total += estimate_text(block.text)
            elif isinstance(block, ToolUseBlock):
                total += _TOOL_USE_OVERHEAD + estimate_text(block.name)
                total += estimate_text(repr(block.arguments))
            elif isinstance(block, ToolResultBlock):
                total += estimate_text(block.content) + 8
            elif isinstance(block, ImageBlock):
                if block.data is None:
                    total += estimate_text(block.summary or "")
                else:
                    # base64 长度 / 750 是对像素数的粗略反推
                    total += min(_IMAGE_FALLBACK_TOKENS, max(200, len(block.data) // 750))
        message.token_estimate = total
        return total

    def estimate(self, messages: Sequence[Message]) -> int:
        return sum(self.estimate_message(m) for m in messages)

    async def count_exact(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        return None


class CalibratedTokenEstimator:
    """启发式 + 精确计数校准。

    `estimate()` 永远是同步且便宜的；`count_exact()` 打一次网络，
    并把 exact/heuristic 的比值滑动平均进 `scale`，让后续估算逐步收敛。
    """

    def __init__(
        self,
        client: LlmClient,
        *,
        base: TokenEstimator | None = None,
        smoothing: float = 0.3,
    ) -> None:
        self._client = client
        self._base = base or HeuristicTokenEstimator()
        self._smoothing = smoothing
        self.scale = 1.0
        self.calibrations = 0

    def estimate_message(self, message: Message) -> int:
        return int(self._base.estimate_message(message) * self.scale)

    def estimate(self, messages: Sequence[Message]) -> int:
        return int(self._base.estimate(messages) * self.scale)

    async def count_exact(
        self,
        messages: Sequence[Message],
        *,
        model_config: ModelConfig,
        tools: Sequence[ToolSpec] = (),
    ) -> int | None:
        exact = await self._client.count_tokens(
            messages, model_config=model_config, tools=tools
        )
        if exact is None:
            return None
        raw = self._base.estimate(messages)
        if raw > 0:
            observed = exact / raw
            self.scale = (1 - self._smoothing) * self.scale + self._smoothing * observed
            self.calibrations += 1
        return exact
