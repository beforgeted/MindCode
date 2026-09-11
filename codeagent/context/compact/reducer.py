from __future__ import annotations

import json
from dataclasses import replace

from codeagent.context.compact.map_summarizer import _call_json
from codeagent.context.compact.models import TaskCheckpoint, TaskDelta
from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message
from codeagent.llm.types import ModelConfig

_REDUCE_SYSTEM = """Merge an existing task checkpoint with new task deltas.
Return exactly one JSON object matching the checkpoint schema and no commentary.
This is state reduction, not prose summarization. Preserve existing constraints,
decisions, failed attempts and evidence unless a delta explicitly supersedes them.
Deduplicate semantically identical entries. Prefer newer file/test state for the same
path/name. Keep evidence references compact; never invent facts or copy tool output.
Required keys: goal, constraints, decisions, completed_work, files, tests,
failed_attempts, open_issues, next_steps, evidence_refs, version, updated_at."""


class TaskStateReducer:
    def __init__(self, client: LlmClient, model_config: ModelConfig) -> None:
        self._client = client
        self._model_config = model_config

    async def reduce(
        self,
        checkpoint: TaskCheckpoint | None,
        deltas: tuple[TaskDelta, ...],
        *,
        max_output_tokens: int,
        focus: str | None,
    ) -> TaskCheckpoint:
        config = replace(
            self._model_config,
            max_output_tokens=max_output_tokens,
            temperature=0.0,
        )
        payload = {
            "focus": focus,
            "existing_checkpoint": json.loads(checkpoint.to_json()) if checkpoint else None,
            "deltas": [json.loads(_delta_json(delta)) for delta in deltas],
            "next_version": (checkpoint.version + 1) if checkpoint else 1,
        }
        prompt = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        messages = (Message.system(_REDUCE_SYSTEM), Message.user(prompt))
        result = await _call_json(
            self._client,
            messages,
            config,
            TaskCheckpoint.from_json,
        )
        expected_version = payload["next_version"]
        if result.version != expected_version:
            raise ValueError(
                f"checkpoint version {result.version} != expected {expected_version}"
            )
        return result


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
