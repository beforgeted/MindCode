from __future__ import annotations

import json
from dataclasses import dataclass, replace

from codeagent.context.compact.map_summarizer import _call_json
from codeagent.context.compact.models import TaskCheckpoint, TaskDelta
from codeagent.context.compact.request_budget import (
    CompactionBudgetError,
    request_fits,
)
from codeagent.context.token_estimator import HeuristicTokenEstimator, TokenEstimator
from codeagent.infra.trace import trace_scope
from codeagent.llm.client import LlmClient, effective_model_config
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig

_REDUCE_SYSTEM = """Merge an existing task checkpoint with new task deltas.
Return exactly one JSON object matching the checkpoint schema and no commentary.
This is state reduction, not prose summarization. Preserve existing constraints,
decisions, failed attempts and evidence unless a delta explicitly supersedes them.
Deduplicate semantically identical entries. Prefer newer file/test state for the same
path/name. Keep evidence references compact; never invent facts or copy tool output.
Required keys: goal, constraints, decisions, completed_work, files, tests,
failed_attempts, open_issues, next_steps, evidence_refs, version, updated_at.
Use exactly next_version; intermediate batch checkpoints can have the same version."""


@dataclass
class ReduceBudget:
    limit: int = 32
    used: int = 0

    def __post_init__(self) -> None:
        if type(self.limit) is not int or self.limit <= 0:
            raise ValueError('Reduce batch limit must be a positive integer')

    def consume(self) -> None:
        if self.used >= self.limit:
            raise CompactionBudgetError('Reduce 总批次预算耗尽')
        self.used += 1


class TaskStateReducer:
    def __init__(self, client: LlmClient, model_config: ModelConfig,
                 estimator: TokenEstimator | None = None) -> None:
        self._client = client
        self._model_config = model_config
        self._estimator = estimator or HeuristicTokenEstimator()

    async def reduce(
        self,
        checkpoint: TaskCheckpoint | None,
        deltas: tuple[TaskDelta, ...],
        *,
        max_output_tokens: int,
        focus: str | None,
        budget: ReduceBudget | None = None,
    ) -> TaskCheckpoint:
        config = effective_model_config(self._client, replace(
            self._model_config,
            max_output_tokens=max_output_tokens,
            temperature=0.0,
        ))
        budget = budget or ReduceBudget()
        version = checkpoint.version + 1 if checkpoint else 1
        current = checkpoint
        position = 0
        first = True
        # Checkpoints remain local until every ordered delta has been consumed.
        while first or position < len(deltas):
            first = False
            batch: tuple[TaskDelta, ...] = ()
            for delta in deltas[position:]:
                candidate = (*batch, delta)
                if not request_fits(self._messages(current, candidate, focus, version),
                                    config, self._estimator):
                    break
                batch = candidate
            if not batch and position < len(deltas):
                raise CompactionBudgetError('Reduce 检查点与单个 delta 无法容纳模型窗口')
            messages = self._messages(current, batch, focus, version)
            if not request_fits(messages, config, self._estimator):
                raise CompactionBudgetError('Reduce 检查点或固定提示词超过模型窗口')
            budget.consume()
            with trace_scope(compaction_phase='reduce', compaction_batch=budget.used,
                             compaction_batch_deltas=len(batch), checkpoint_version=version):
                result = await _call_json(self._client, messages, config, TaskCheckpoint.from_json,
                                          estimator=self._estimator)
            if result.version != version:
                raise ValueError(f'checkpoint version {result.version} != expected {version}')
            current = result
            position += len(batch)
        assert current is not None
        return current

    @staticmethod
    def _messages(checkpoint: TaskCheckpoint | None, deltas: tuple[TaskDelta, ...],
                  focus: str | None, version: int) -> tuple[Message, ...]:
        payload = {
            "focus": focus,
            "existing_checkpoint": json.loads(checkpoint.to_json()) if checkpoint else None,
            "deltas": [json.loads(_delta_json(delta)) for delta in deltas],
            "next_version": version,
        }
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return (Message.system(_REDUCE_SYSTEM), Message.user(prompt))


def _delta_json(delta: TaskDelta) -> str:
    checkpoint = TaskCheckpoint(
        goal=delta.goal,
        constraints=delta.constraints,
        decisions=delta.decisions,
        completed_work=delta.completed_work,
        files=delta.files,
        tests=delta.tests,
        failed_attempts=delta.failed_attempts,
        open_issues=delta.open_issues,
        next_steps=delta.next_steps,
        evidence_refs=delta.evidence_refs,
    )
    data = json.loads(checkpoint.to_json())
    data.pop("version", None)
    data.pop("updated_at", None)
    return json.dumps(data, ensure_ascii=False, sort_keys=True)
