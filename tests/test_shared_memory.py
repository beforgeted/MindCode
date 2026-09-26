from __future__ import annotations

from pathlib import Path

from codeagent.agent.models import (
    AgentDefinition,
    AgentRunResult,
    MemoryProfile,
)
from codeagent.agent.run import AgentRun
from codeagent.evidence.models import EvidenceRef, EvidenceType
from codeagent.memory.governance_models import (
    MemoryCandidate,
    candidate_key,
    content_hash,
)
from codeagent.memory.models import MemorySource, MemoryType
from codeagent.orchestration.shared_memory import (
    NullSupervisorMemoryWriter,
    SupervisorMemoryWriter,
)
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.workspace.context import WorkspaceContext


class FakeSink:
    def __init__(self) -> None:
        self.staged: list[MemoryCandidate] = []

    async def stage_shared_candidates(self, candidates: tuple[MemoryCandidate, ...]) -> int:
        # 幂等去重语义：已存在的 candidate_key 不重复计入。
        existing = {c.candidate_key for c in self.staged}
        fresh = [c for c in candidates if c.candidate_key not in existing]
        self.staged.extend(fresh)
        return len(fresh)


def _candidate(content: str, *, type_: MemoryType = MemoryType.FACT) -> MemoryCandidate:
    event_ids = ("ev_1",)
    key = candidate_key(
        project_id="p",
        source_event_ids=event_ids,
        source=MemorySource.ASSISTANT_DERIVED,
        proposed_scope=MemoryCandidate.model_fields["proposed_scope"].default,
        proposed_type=type_,
        content=content,
    )
    return MemoryCandidate(
        candidate_key=key,
        project_id="p",
        session_id="s",
        content=content,
        content_sha256=content_hash(content),
        source=MemorySource.ASSISTANT_DERIVED,
        proposed_type=type_,
        evidence_refs=(EvidenceRef(type=EvidenceType.MESSAGE, event_id="ev_1"),),
        source_event_ids=event_ids,
        reason="test",
    )


def _worker(
    step_id: str,
    candidates: tuple[MemoryCandidate, ...],
    *,
    profile: MemoryProfile | None = None,
) -> WorkerRun:
    defn = AgentDefinition(
        id="w",
        name="W",
        system_prompt="",
        memory_profile=profile or MemoryProfile(),
    )
    run = AgentRun.create(defn, session_id="s", workspace=WorkspaceContext.local(Path(".")))
    result = AgentRunResult.success(run.run_id, "ok", memory_candidates=candidates)
    return WorkerRun(
        step_id=step_id,
        run=run,
        result=result,
        workspace=run.workspace,
        verification=VerificationResult(ok=True),
    )


async def test_supervisor_dedups_across_workers():
    sink = FakeSink()
    writer = SupervisorMemoryWriter(sink)
    dup = _candidate("共享事实")
    workers = {
        "a": _worker("a", (dup, _candidate("独有 A"))),
        "b": _worker("b", (dup, _candidate("独有 B"))),  # dup 与 a 重复
    }
    report = await writer.collect_and_stage(workers)
    assert report.collected == 4
    assert report.staged == 3  # dup 去重
    # 只 staging，未直接落 ACTIVE：sink 收到的都是候选。
    assert len(sink.staged) == 3


async def test_writable_types_filter_rejects_out_of_scope():
    sink = FakeSink()
    writer = SupervisorMemoryWriter(sink)
    profile = MemoryProfile(writable_types=(MemoryType.FACT,))
    workers = {
        "a": _worker(
            "a",
            (
                _candidate("允许的事实", type_=MemoryType.FACT),
                _candidate("越界偏好", type_=MemoryType.PREFERENCE),
            ),
            profile=profile,
        ),
    }
    report = await writer.collect_and_stage(workers)
    assert report.collected == 2
    assert report.rejected == 1
    assert report.staged == 1
    assert all(c.proposed_type is MemoryType.FACT for c in sink.staged)


async def test_null_writer_stages_nothing():
    writer = NullSupervisorMemoryWriter()
    workers = {"a": _worker("a", (_candidate("x"),))}
    report = await writer.collect_and_stage(workers)
    assert report.staged == 0
    assert report.collected == 0


async def test_sink_failure_is_conservative():
    class BoomSink:
        async def stage_shared_candidates(self, candidates):
            raise RuntimeError("db down")

    writer = SupervisorMemoryWriter(BoomSink())
    workers = {"a": _worker("a", (_candidate("x"),))}
    report = await writer.collect_and_stage(workers)
    assert report.collected == 1
    assert report.staged == 0  # 保守失败，不抛
