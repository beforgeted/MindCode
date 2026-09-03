"""ImagePayloadPruner。

上下文文档 §14：图片成本高，历史图片的 base64 payload 应该裁掉。但**不能完全删除**
—— 否则模型不知道之前那张图提供了什么信息。所以：

    图片本体  -> 只在最近 N 个 turn 保留
    图片描述  -> 后续 turn 一直保留

理想情况下描述来自 vision 调用的结果；现在先留结构化占位，
等 P2 接 vision summary 时只需要填 `summary` 字段，调用方不用改。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from codeagent.context.token_estimator import TokenEstimator
from codeagent.llm.message import Block, ImageBlock, Message


@dataclass(frozen=True, slots=True)
class PruneOutcome:
    messages: tuple[Message, ...]
    tokens_removed: int
    items_pruned: int


class ImagePayloadPruner:
    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def prune(
        self,
        messages: Sequence[Message],
        *,
        hot_turn_ids: frozenset[str],
    ) -> PruneOutcome:
        out: list[Message] = []
        removed = 0
        pruned = 0
        for message in messages:
            images = message.images
            if not images or (message.turn_id is not None and message.turn_id in hot_turn_ids):
                out.append(message)
                continue
            if all(image.pruned for image in images):
                out.append(message)
                continue
            before = self._estimator.estimate_message(message)
            blocks: list[Block] = []
            for block in message.blocks:
                if isinstance(block, ImageBlock) and not block.pruned:
                    blocks.append(
                        ImageBlock(
                            media_type=block.media_type,
                            data=None,
                            summary=block.summary or _placeholder(block),
                        )
                    )
                    pruned += 1
                else:
                    blocks.append(block)
            new_message = message.with_blocks(blocks)
            removed += before - self._estimator.estimate_message(new_message)
            out.append(new_message)
        return PruneOutcome(tuple(out), max(0, removed), pruned)


def _placeholder(block: ImageBlock) -> str:
    size = len(block.data or "")
    return (
        f"历史图片（{block.media_type}，base64 约 {size} 字符）payload 已裁剪，"
        "未生成描述。需要时请用户重新提供。"
    )
