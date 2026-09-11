from __future__ import annotations

import json

import pytest

from codeagent.context.compact.models import (
    CompactionPayloadError,
    TaskCheckpoint,
    TaskDelta,
)
from codeagent.llm.message import ContextCategory, Role


def _payload() -> dict:
    return {
        "goal": "finish P2",
        "constraints": ["preserve protocol", "preserve protocol"],
        "decisions": [{"decision": "use typed state", "rationale": "avoid drift"}],
        "completed_work": ["turn lifecycle"],
        "files": [{"path": "codeagent/context", "change": "modified"}],
        "tests": [{"name": "pytest", "outcome": "pass", "detail": "13 passed"}],
        "failed_attempts": [{"attempt": "raw truncation", "why_failed": "loses state"}],
        "open_issues": ["memory"],
        "next_steps": ["wire compactor"],
        "evidence_refs": [{"type": "artifact", "artifact_uri": "artifact://log/1"}],
    }


def test_checkpoint_round_trip_and_projection():
    payload = _payload()
    payload.update({"version": 3, "updated_at": "2026-09-10T00:00:00+00:00"})
    checkpoint = TaskCheckpoint.from_json(json.dumps(payload))
    restored = TaskCheckpoint.from_json(checkpoint.to_json())
    message = checkpoint.to_message()

    assert restored == checkpoint
    assert checkpoint.constraints == ("preserve protocol",)
    assert message.role is Role.INTERNAL_CONTEXT
    assert message.category is ContextCategory.CHECKPOINT
    assert "raw truncation" in message.text
    assert "artifact://log/1" in message.text


def test_delta_accepts_single_json_fence():
    delta = TaskDelta.from_json("```json\n" + json.dumps(_payload()) + "\n```")
    assert delta.goal == "finish P2"


@pytest.mark.parametrize(
    "payload",
    [
        "{} trailing",
        "[]",
        json.dumps({**_payload(), "surprise": True}),
        "```json\n{}",
    ],
)
def test_strict_parser_rejects_invalid_payload(payload: str):
    with pytest.raises(CompactionPayloadError):
        TaskDelta.from_json(payload)
