"""Evidence 平面的数据模型。

裁决（记忆 V2 §5.2 覆盖上下文文档 §2.1）：

    RawEventStore        = Durable Historical Truth（持久事实源）
    ConversationHistory  = Active LLM Working History（运行工作集）

ConversationHistory 可以被 prune / offload / compact / 换成 Checkpoint，
RawEventStore 不受这些操作影响。所以 EvidenceRef 一律指向 EventStore
或 ArtifactStore，不指向 History。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from codeagent.infra.ids import new_event_id


class EventType(StrEnum):
    USER_MESSAGE = "user_message"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    AGENT_RUN_STARTED = "agent_run_started"
    AGENT_RUN_FINISHED = "agent_run_finished"
    TURN_STARTED = "turn_started"
    TURN_FINISHED = "turn_finished"
    CONTEXT_PREPARED = "context_prepared"
    COMPACTION = "compaction"
    FILE_CHANGED = "file_changed"
    TEST_RESULT = "test_result"
    CHECKPOINT_CREATED = "checkpoint_created"
    MEMORY_CREATED = "memory_created"
    MEMORY_UPDATED = "memory_updated"


@dataclass(frozen=True, slots=True)
class AgentEvent:
    type: EventType
    session_id: str
    event_id: str = field(default_factory=new_event_id)
    agent_run_id: str | None = None
    tool_run_id: str | None = None
    turn_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    payload: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "type": str(self.type),
            "session_id": self.session_id,
            "agent_run_id": self.agent_run_id,
            "tool_run_id": self.tool_run_id,
            "turn_id": self.turn_id,
            "created_at": self.created_at.isoformat(),
            "payload": self.payload,
        }


class EvidenceType(StrEnum):
    MESSAGE = "message"
    TOOL_RESULT = "tool_result"
    ARTIFACT = "artifact"
    DIFF = "diff"
    TEST_LOG = "test_log"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    type: EvidenceType
    event_id: str | None = None
    session_id: str | None = None
    agent_run_id: str | None = None
    tool_run_id: str | None = None
    artifact_uri: str | None = None

    def __str__(self) -> str:
        parts = [str(self.type)]
        for label, value in (
            ("session", self.session_id),
            ("run", self.agent_run_id),
            ("toolrun", self.tool_run_id),
            ("event", self.event_id),
            ("artifact", self.artifact_uri),
        ):
            if value:
                parts.append(f"{label}={value}")
        return " ".join(parts)


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    path: Path
    size_bytes: int
    media_type: str = "text/plain"

    @property
    def uri(self) -> str:
        return f"artifact://{self.kind}/{self.artifact_id}"

    @staticmethod
    def parse_uri(uri: str) -> tuple[str, str]:
        """artifact://<kind>/<id> -> (kind, id)"""
        rest = uri.removeprefix("artifact://")
        kind, _, artifact_id = rest.partition("/")
        return kind, artifact_id
