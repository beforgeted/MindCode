"""Agent 层数据模型。

AgentDefinition 只描述静态能力（Agent 是谁、会什么），绝不放 history /
current_step / retry_count / last_tool_result —— 那些属于 AgentRun / RunContext。

AgentRunResult 用的是记忆 V2 §32 的**结构化**形状，而不是 Multi-Agent 文档
§9 那个 `AgentResult.success(run_id, content)` 的字符串形状。后者无法满足
记忆 V2 §47.7：Worker 产 80K tool history，Master 只收结构化结果。
Worker 的过程细节通过 EvidenceRef 下钻，不进 Master 的 Context。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from codeagent.context.profile import ContextProfile
from codeagent.evidence.models import EvidenceRef
from codeagent.llm.types import ModelConfig


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MAX_ITERATIONS = "max_iterations"


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    id: str
    name: str
    system_prompt: str
    model_config: ModelConfig = field(default_factory=ModelConfig)
    allowed_tools: tuple[str, ...] = ()
    max_react_iterations: int = 25
    max_reflection_count: int = 3
    context_profile: ContextProfile = field(default_factory=ContextProfile)


class FileChangeKind(StrEnum):
    CREATED = "created"
    MODIFIED = "modified"
    DELETED = "deleted"


@dataclass(frozen=True, slots=True)
class FileState:
    path: str
    change: FileChangeKind


class TestOutcome(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIPPED = "skipped"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class TestState:
    name: str
    outcome: TestOutcome
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DecisionState:
    decision: str
    rationale: str | None = None


@dataclass(frozen=True, slots=True)
class FailedAttempt:
    attempt: str
    why_failed: str


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    run_id: str
    status: RunStatus
    summary: str
    decisions: tuple[DecisionState, ...] = ()
    files: tuple[FileState, ...] = ()
    tests: tuple[TestState, ...] = ()
    open_issues: tuple[str, ...] = ()
    evidence_refs: tuple[EvidenceRef, ...] = ()
    # P3/P4 挂载点：Worker 只产候选，不直接写 PROJECT Memory。
    memory_candidates: tuple[object, ...] = ()
    error: str | None = None
    iterations: int = 0

    @property
    def ok(self) -> bool:
        return self.status is RunStatus.SUCCESS

    @classmethod
    def success(cls, run_id: str, summary: str, **kwargs) -> AgentRunResult:
        return cls(run_id=run_id, status=RunStatus.SUCCESS, summary=summary, **kwargs)

    @classmethod
    def failed(cls, run_id: str, error: str, **kwargs) -> AgentRunResult:
        return cls(
            run_id=run_id, status=RunStatus.FAILED, summary="", error=error, **kwargs
        )
