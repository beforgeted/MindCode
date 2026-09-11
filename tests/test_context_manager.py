"""ContextManager：裁剪顺序、归因、以及"绝不静默截断"。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from codeagent.context.compact.base import CompactionResult
from codeagent.context.compact.models import TaskCheckpoint
from codeagent.context.history.conversation_history import ConversationHistory
from codeagent.context.manager import ContextManager, ContextOverflowError
from codeagent.context.profile import ContextProfile
from codeagent.llm.message import (
    ContextCategory,
    ImageBlock,
    Message,
    Role,
    ToolResultBlock,
    ToolUseBlock,
)


def _history() -> ConversationHistory:
    return ConversationHistory(session_id="ses", agent_run_id="run")


def _turn(history: ConversationHistory, *, tool_output: str, image: str | None = None) -> None:
    turn_id = history.begin_turn()
    history.append(Message.user("请继续", turn_id=turn_id))
    if image is not None:
        history.append(
            Message(
                role=Role.USER,
                blocks=(ImageBlock("image/png", data=image),),
                category=ContextCategory.IMAGE,
                turn_id=turn_id,
            )
        )
    history.append(Message.assistant([ToolUseBlock(f"tu_{turn_id}", "echo", {})], turn_id=turn_id))
    history.append(
        Message.tool([ToolResultBlock(f"tu_{turn_id}", tool_output)], turn_id=turn_id)
    )
    history.end_turn()


async def test_old_tool_results_are_degraded_recent_are_not(profile: ContextProfile):
    history = _history()
    history.append(Message.system("system prompt"))
    for index in range(5):
        _turn(history, tool_output=f"tool output {index} " + "x" * 4_000)

    manager = ContextManager()
    result = await manager.prepare(history, profile)

    tool_messages = [m for m in result.messages if m.tool_results]
    assert len(tool_messages) == 5
    # 最新 turn（HOT）保持完整
    assert not tool_messages[-1].tool_results[0].truncated
    # 最旧 turn 被降级
    assert tool_messages[0].tool_results[0].truncated
    assert "已降级" in tool_messages[0].tool_results[0].content
    assert result.tool_tokens_removed > 0


async def test_image_payload_pruned_but_summary_kept(profile: ContextProfile):
    history = _history()
    _turn(history, tool_output="small", image="A" * 200_000)
    _turn(history, tool_output="small")

    manager = ContextManager()
    result = await manager.prepare(history, profile)

    images = [block for m in result.messages for block in m.images]
    assert images and images[0].pruned
    # 完全删除会让模型不知道之前那张图提供了什么 —— 必须留描述。
    assert images[0].summary
    assert result.image_tokens_removed > 0


async def test_pruning_is_idempotent(profile: ContextProfile):
    history = _history()
    for index in range(5):
        _turn(history, tool_output=f"out {index} " + "y" * 4_000)

    manager = ContextManager()
    first = await manager.prepare(history, profile)
    second = await manager.prepare(history, profile)

    assert second.tokens_before == first.tokens_final
    assert second.tool_tokens_removed == 0


async def test_breakdown_sums_to_total(profile: ContextProfile):
    history = _history()
    history.append(Message.system("system prompt"))
    _turn(history, tool_output="out " + "z" * 2_000)

    manager = ContextManager()
    result = await manager.prepare(history, profile)

    assert ContextCategory.SYSTEM in result.breakdown
    assert ContextCategory.TOOL_RESULT in result.breakdown
    assert sum(result.breakdown.values()) == result.tokens_final


async def test_overflow_raises_instead_of_silent_truncation():
    """P1 没有 Compactor，越过 hard limit 必须响亮失败。

    宁可失败，也不能 `messages[-N:]` 静默丢掉用户约束和架构决策。
    """
    tiny = replace(ContextProfile(), context_window=2_000, max_tool_result_tokens=100_000)
    history = _history()
    turn_id = history.begin_turn()
    history.append(Message.user("x" * 40_000, turn_id=turn_id))

    manager = ContextManager()
    with pytest.raises(ContextOverflowError) as exc:
        await manager.prepare(history, tiny)
    assert "hard limit" in str(exc.value)
    # 历史没被破坏
    assert len(history) == 1


async def test_prediction_triggers_before_current_exceeds_soft():
    profile = replace(
        ContextProfile(),
        context_window=100_000,
        expected_tool_burst=30_000,
        output_reserve=16_000,
        safety_margin=5_000,
    )
    history = _history()
    turn_id = history.begin_turn()
    # 约 45K tokens：低于 soft(80K)，但 45+30+16+5 = 96K 已越过 hard(92K)。
    history.append(Message.user("字" * 45_000, turn_id=turn_id))

    manager = ContextManager()
    result = await manager.prepare(history, profile)
    assert result.prediction.current_tokens < result.prediction.soft_trigger
    assert result.prediction.should_compact
    assert "下一轮风险" in result.prediction.reason


class SuccessfulCompactor:
    async def compact(
        self,
        messages,
        *,
        profile,
        focus=None,
        checkpoint=None,
        turn_statuses=None,
    ):
        next_checkpoint = TaskCheckpoint(goal="keep working")
        assert turn_statuses is not None
        recent_turn_id = list(turn_statuses)[-1]
        candidate = tuple(
            [message for message in messages if message.role is Role.SYSTEM]
            + [next_checkpoint.to_message()]
            + [message for message in messages if message.turn_id == recent_turn_id]
        )
        return CompactionResult(
            compacted=True,
            messages=candidate,
            checkpoint=next_checkpoint,
            tokens_before=1_000,
            tokens_after=100,
        )


async def test_context_manager_atomically_commits_checkpoint(profile: ContextProfile):
    history = _history()
    history.append(Message.system("system"))
    _turn(history, tool_output="old")
    _turn(history, tool_output="recent")
    manager = ContextManager(compactor=SuccessfulCompactor())

    result = await manager.prepare(history, profile, force_compact=True)

    assert result.compacted
    assert result.compaction is not None
    assert history.checkpoint is result.compaction.checkpoint
    assert history.compaction_count == 1
    assert ContextCategory.CHECKPOINT in result.breakdown
    assert history.messages == result.messages


class InvalidCandidateCompactor(SuccessfulCompactor):
    async def compact(self, messages, **kwargs):
        checkpoint = TaskCheckpoint(goal="invalid")
        return CompactionResult(
            compacted=True,
            messages=(checkpoint.to_message(), Message.user("x" * 100_000)),
            checkpoint=checkpoint,
            tokens_before=100,
            tokens_after=100_000,
        )


async def test_context_manager_rejects_candidate_over_hard_limit():
    profile = replace(ContextProfile(), context_window=2_000)
    history = _history()
    _turn(history, tool_output="small")
    source = history.messages
    manager = ContextManager(compactor=InvalidCandidateCompactor())

    result = await manager.prepare(history, profile, force_compact=True)

    assert not result.compacted
    assert result.compaction is not None
    assert "未提交" in result.compaction.reason
    assert history.messages == source
    assert history.checkpoint is None
    assert history.compaction_count == 0
