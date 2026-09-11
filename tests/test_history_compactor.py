from __future__ import annotations

import asyncio
import json
from dataclasses import replace

from codeagent.context.compact.history_compactor import ConversationHistoryCompactor
from codeagent.context.compact.models import TaskCheckpoint
from codeagent.context.history.conversation_history import (
    ConversationHistory,
    validate_tool_protocol,
)
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.infra.ids import new_llm_call_id
from codeagent.llm.message import Message, ToolResultBlock, ToolUseBlock
from codeagent.llm.types import LlmResponse, ModelConfig


def _delta(*, goal: str = "ship P2", constraint: str = "never split tools") -> str:
    return json.dumps(
        {
            "goal": goal,
            "constraints": [constraint],
            "decisions": [],
            "completed_work": ["old work"],
            "files": [],
            "tests": [],
            "failed_attempts": [],
            "open_issues": [],
            "next_steps": ["continue"],
            "evidence_refs": [],
        }
    )


def _checkpoint(version: int, *, constraint: str = "never split tools") -> str:
    data = json.loads(_delta(constraint=constraint))
    data.update({"version": version, "updated_at": "2026-09-10T00:00:00+00:00"})
    return json.dumps(data)


class CompactionClient:
    def __init__(
        self,
        *,
        fail_first_map: bool = False,
        fail_reduce: bool = False,
        delay: float = 0.0,
    ) -> None:
        self.fail_first_map = fail_first_map
        self.fail_reduce = fail_reduce
        self.delay = delay
        self.map_calls = 0
        self.reduce_calls = 0
        self.models: list[str] = []

    async def chat(self, messages, *, model_config, tools=()):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.models.append(model_config.model)
        is_map = model_config.model == model_config.map_model
        if is_map:
            self.map_calls += 1
            if self.fail_first_map and self.map_calls <= 2:
                content = "not json"
            else:
                content = _delta(constraint=f"constraint-{self.map_calls}")
        else:
            self.reduce_calls += 1
            content = "not json" if self.fail_reduce else _checkpoint(self.reduce_calls)
        return LlmResponse(new_llm_call_id(), content, stop_reason="end_turn")

    async def count_tokens(self, messages, *, model_config, tools=()):
        return None


def _add_turn(history: ConversationHistory, index: int, *, complete: bool = True) -> str:
    turn_id = history.begin_turn()
    history.append(Message.user(f"request {index} " + "x" * 500, turn_id=turn_id))
    history.append(
        Message.assistant(
            [
                ToolUseBlock(f"tu_{index}_a", "echo", {}),
                ToolUseBlock(f"tu_{index}_b", "echo", {}),
            ],
            turn_id=turn_id,
        )
    )
    history.append(
        Message.tool(
            [
                ToolResultBlock(f"tu_{index}_a", "result a " + "y" * 500),
                ToolResultBlock(f"tu_{index}_b", "result b " + "z" * 500),
            ],
            turn_id=turn_id,
        )
    )
    if complete:
        history.end_turn()
    return turn_id


async def test_compacts_old_turns_and_preserves_recent_and_running():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    history.append(Message.system("system"))
    for index in range(4):
        _add_turn(history, index)
    running_id = _add_turn(history, 9, complete=False)
    client = CompactionClient()
    estimator = HeuristicTokenEstimator()
    compactor = ConversationHistoryCompactor(
        client, estimator, ModelConfig(model="main", map_model="map")
    )
    profile = replace(
        ContextProfile(),
        context_window=20_000,
        retain_recent_turns=1,
        map_chunk_tokens=900,
    )

    result = await compactor.compact(
        history.messages,
        profile=profile,
        checkpoint=history.checkpoint,
        turn_statuses=history.turn_statuses,
    )

    assert result.compacted
    assert result.checkpoint is not None
    assert result.messages[0].role.value == "system"
    assert result.messages[1].category.value == "checkpoint"
    assert any(message.turn_id == running_id for message in result.messages)
    assert any(message.turn_id == list(history.turn_statuses)[-2] for message in result.messages)
    validate_tool_protocol(result.messages)
    assert "map" in client.models and "main" in client.models


async def test_partial_map_failure_keeps_entire_original_chunk():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    for index in range(4):
        _add_turn(history, index)
    original_first = history.messages[:3]
    client = CompactionClient(fail_first_map=True)
    compactor = ConversationHistoryCompactor(
        client,
        HeuristicTokenEstimator(),
        ModelConfig(model="main", map_model="map"),
    )
    profile = replace(
        ContextProfile(),
        context_window=20_000,
        retain_recent_turns=1,
        map_chunk_tokens=600,
    )

    result = await compactor.compact(
        history.messages, profile=profile, turn_statuses=history.turn_statuses
    )

    assert result.compacted
    assert result.map_failures == 1
    assert all(message in result.messages for message in original_first)
    validate_tool_protocol(result.messages)


async def test_reduce_failure_returns_original_history():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    for index in range(2):
        _add_turn(history, index)
    source = history.messages
    client = CompactionClient(fail_reduce=True)
    compactor = ConversationHistoryCompactor(
        client,
        HeuristicTokenEstimator(),
        ModelConfig(model="main", map_model="map"),
    )
    profile = replace(ContextProfile(), retain_recent_turns=0)

    result = await compactor.compact(
        source, profile=profile, turn_statuses=history.turn_statuses
    )

    assert not result.compacted
    assert result.messages == source
    assert history.checkpoint is None
    assert history.compaction_count == 0


async def test_timeout_returns_original_history():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    for index in range(2):
        _add_turn(history, index)
    source = history.messages
    compactor = ConversationHistoryCompactor(
        CompactionClient(delay=0.05),
        HeuristicTokenEstimator(),
        ModelConfig(model="main", map_model="map"),
    )
    profile = replace(
        ContextProfile(),
        retain_recent_turns=0,
        compaction_timeout_seconds=0.001,
    )

    result = await compactor.compact(
        source, profile=profile, turn_statuses=history.turn_statuses
    )

    assert not result.compacted
    assert result.messages == source
    assert "超时" in result.reason


def test_history_apply_compaction_is_atomic():
    history = ConversationHistory(session_id="ses", agent_run_id="run")
    _add_turn(history, 1)
    source = history.messages
    checkpoint = TaskCheckpoint(goal="goal")

    try:
        history.apply_compaction((Message.user("missing projection"),), checkpoint)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid compaction should fail")

    assert history.messages == source
    assert history.checkpoint is None
    assert history.compaction_count == 0
