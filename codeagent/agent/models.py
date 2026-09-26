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
from codeagent.memory.governance_models import MemoryCandidate
from codeagent.memory.models import MemoryType


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"
    MAX_ITERATIONS = "max_iterations"


@dataclass(frozen=True, slots=True)
class MemoryProfile:
    """专项 Agent 的 Memory 读写边界（P6）。

    默认全放行——现有单 Agent 无需改动即向后兼容。空 tuple = 不设限。
    """

    # 该 Agent 检索时只注入这些类型的 Memory（空 = 全部可读）
    readable_types: tuple[MemoryType, ...] = ()
    # 该 Agent 能产出的候选类型（空 = 全部可写）；越界候选由 Supervisor 丢弃
    writable_types: tuple[MemoryType, ...] = ()
    # 覆盖 ContextProfile.max_memory_injection_tokens（None = 用 profile 默认）
    max_injection_tokens: int | None = None

    def can_read(self, type_: MemoryType) -> bool:
        return not self.readable_types or type_ in self.readable_types

    def can_write(self, type_: MemoryType) -> bool:
        return not self.writable_types or type_ in self.writable_types


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
    memory_profile: MemoryProfile = field(default_factory=MemoryProfile)


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
    # P6：Worker 只产候选，不直接写 PROJECT Memory；由 Supervisor 集中 staging。
    memory_candidates: tuple[MemoryCandidate, ...] = ()
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
