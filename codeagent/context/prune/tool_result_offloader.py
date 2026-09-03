"""ToolResultOffloader：历史 tool 结果的 HOT / WARM / COLD 生命周期。

这是**裁剪**，不是摘要。上下文文档 §40.2 定的顺序是

    Image Pruning -> Tool Result Pruning -> History Compaction

裁剪是确定性的、零 LLM 调用、无有损风险，所以先吃掉这部分收益，
把昂贵且有风险的 Compaction 留作最后一道防线。

Coding Agent 最容易膨胀的从来不是对话，而是 read_file / grep / mvn test /
git diff 的输出，所以这一层的收益通常比压缩更大且更便宜。

必须幂等：每轮都会跑一次，已经降级过的结果不能被反复再截断。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import TokenEstimator
from codeagent.infra.text import bounded_preview, count_lines, extract_key_lines
from codeagent.llm.message import Block, Message, ToolResultBlock


class ResultTier(StrEnum):
    HOT = "hot"
    WARM = "warm"
    COLD = "cold"


@dataclass(frozen=True, slots=True)
class OffloadOutcome:
    messages: tuple[Message, ...]
    tokens_removed: int
    items_offloaded: int


class ToolResultOffloader:
    def __init__(self, estimator: TokenEstimator) -> None:
        self._estimator = estimator

    def offload(
        self,
        messages: Sequence[Message],
        *,
        profile: ContextProfile,
        turn_order: Sequence[str],
    ) -> OffloadOutcome:
        tiers = _tier_map(turn_order, profile)
        out: list[Message] = []
        removed = 0
        count = 0
        for message in messages:
            results = message.tool_results
            if not results:
                out.append(message)
                continue
            tier = tiers.get(message.turn_id or "", ResultTier.COLD)
            if tier is ResultTier.HOT:
                out.append(message)
                continue
            allowance = (
                profile.tool_result_warm_max_chars
                if tier is ResultTier.WARM
                else profile.tool_result_cold_max_chars
            )
            if all(len(block.content) <= allowance for block in results):
                out.append(message)
                continue
            before = self._estimator.estimate_message(message)
            blocks: list[Block] = []
            for block in message.blocks:
                if isinstance(block, ToolResultBlock) and len(block.content) > allowance:
                    blocks.append(_degrade(block, allowance, tier))
                    count += 1
                else:
                    blocks.append(block)
            new_message = message.with_blocks(blocks)
            removed += before - self._estimator.estimate_message(new_message)
            out.append(new_message)
        return OffloadOutcome(tuple(out), max(0, removed), count)


def _tier_map(turn_order: Sequence[str], profile: ContextProfile) -> dict[str, ResultTier]:
    """turn_order 是按时间正序的 turn_id 列表；越靠后越新。"""
    tiers: dict[str, ResultTier] = {}
    total = len(turn_order)
    for index, turn_id in enumerate(turn_order):
        age = total - 1 - index
        if age < profile.tool_result_hot_turns:
            tiers[turn_id] = ResultTier.HOT
        elif age < profile.tool_result_warm_turns:
            tiers[turn_id] = ResultTier.WARM
        else:
            tiers[turn_id] = ResultTier.COLD
    return tiers


def _degrade(block: ToolResultBlock, allowance: int, tier: ResultTier) -> ToolResultBlock:
    """降级单条结果。保留结构化信号 + artifact 引用，丢掉正文体积。"""
    lines = count_lines(block.content)
    key_errors = extract_key_lines(block.content, limit=4 if tier is ResultTier.COLD else 8)

    header = [f"[tool result 已降级为 {tier}] 原始 {len(block.content)} 字符 / {lines} 行"]
    if block.is_error:
        header.append("status: error")
    if key_errors:
        header.append("keyErrors:")
        header.extend(f"  - {line}" for line in key_errors)
    if block.artifact_uri:
        header.append(f"完整内容: {block.artifact_uri}（可用 read_artifact 回读）")
    else:
        header.append("完整内容未落盘（该结果未经 artifact offload）")

    prefix = "\n".join(header)
    body_budget = max(0, allowance - len(prefix) - 2)
    body = bounded_preview(block.content, body_budget) if body_budget > 0 else ""
    content = f"{prefix}\n{body}" if body else prefix
    if len(content) > allowance:
        # 保证幂等：降级结果必须落在 allowance 内，否则下一轮会被反复再截断。
        content = bounded_preview(content, allowance)

    return ToolResultBlock(
        tool_use_id=block.tool_use_id,
        content=content,
        is_error=block.is_error,
        artifact_uri=block.artifact_uri,
        truncated=True,
    )
