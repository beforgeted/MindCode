from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from codeagent.evidence.models import ArtifactRef, EvidenceRef
from codeagent.infra.ids import new_tool_run_id


class ToolConcurrencyMode(StrEnum):
    """不是所有 ToolCall 都能并行。

    READ_ONLY          read_file / grep / list_files / git_log
    EXCLUSIVE_RESOURCE write_file / delete_file / move_file —— 按资源 key 加锁
    SERIAL             git_commit / 装包 / 全项目 build —— 整批退化为顺序执行
    """

    READ_ONLY = "read_only"
    EXCLUSIVE_RESOURCE = "exclusive_resource"
    SERIAL = "serial"


@dataclass(frozen=True, slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


class ToolResultStatus(StrEnum):
    OK = "ok"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class ToolResult:
    call_id: str
    tool_name: str
    status: ToolResultStatus
    content: str
    exit_code: int | None = None
    artifact: ArtifactRef | None = None
    evidence: EvidenceRef | None = None
    truncated: bool = False
    raw_bytes: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_error(self) -> bool:
        return self.status is not ToolResultStatus.OK

    @property
    def artifact_uri(self) -> str | None:
        return self.artifact.uri if self.artifact else None

    @classmethod
    def ok(cls, call: ToolCall, content: str, **kwargs: Any) -> ToolResult:
        return cls(call.id, call.name, ToolResultStatus.OK, content, **kwargs)

    @classmethod
    def error(cls, call: ToolCall, message: str, **kwargs: Any) -> ToolResult:
        return cls(call.id, call.name, ToolResultStatus.ERROR, message, **kwargs)

    @classmethod
    def timeout(cls, call: ToolCall, seconds: float) -> ToolResult:
        return cls(
            call.id,
            call.name,
            ToolResultStatus.TIMEOUT,
            f"工具执行超时（{seconds}s）。已终止。",
        )

    @classmethod
    def cancelled(cls, call: ToolCall) -> ToolResult:
        return cls(call.id, call.name, ToolResultStatus.CANCELLED, "工具调用已取消。")


class ToolRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class ToolRun:
    agent_run_id: str
    call: ToolCall
    tool_run_id: str = field(default_factory=new_tool_run_id)
    status: ToolRunStatus = ToolRunStatus.CREATED
    result: ToolResult | None = None
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None

    def mark_running(self) -> None:
        self.status = ToolRunStatus.RUNNING
        self.started_at = datetime.now(UTC)

    def finish(self, result: ToolResult) -> None:
        self.result = result
        self.finished_at = datetime.now(UTC)
        if result.status is ToolResultStatus.CANCELLED:
            self.status = ToolRunStatus.CANCELLED
        elif result.is_error:
            self.status = ToolRunStatus.FAILED
        else:
            self.status = ToolRunStatus.SUCCESS

    @property
    def duration_ms(self) -> float | None:
        if self.started_at is None or self.finished_at is None:
            return None
        return (self.finished_at - self.started_at).total_seconds() * 1000.0
