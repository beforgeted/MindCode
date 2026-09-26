"""AgentRuntime：在单 Agent 内核（ReActEngine）外套一层 reflection + 就地验证 + workspace。

复用 ReActEngine.run_turn，不重写内核（V1 §3 P5）。workspace 由注入的
WorkspaceManager 分配，但**不在这里 cleanup**——所有权在 MasterRuntime（合并之后）。

reflection：LocalVerifier 不过且还有预算时，追加纠正指令再 run_turn。ReAct 迭代预算
是 per-run 全局的（RunContext.react_iteration 不重置），耗尽即 MAX_ITERATIONS。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from codeagent.agent.models import AgentDefinition, AgentRunResult, RunStatus
from codeagent.agent.run import AgentRun
from codeagent.evidence.event_store import NullEventStore, RawEventStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.infra.ids import new_agent_run_id
from codeagent.infra.metrics import Metrics
from codeagent.memory.governance_models import MemoryCandidate
from codeagent.orchestration.task_graph import Step
from codeagent.runtime.local_verifier import AlwaysPassVerifier, LocalVerifier, VerificationResult
from codeagent.runtime.react_engine import ReActEngine
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.manager import WorkspaceManager


@runtime_checkable
class WorkerCandidateHarvester(Protocol):
    """从一次 Worker 运行里抽取 MemoryCandidate（P6）。

    Worker 只产候选、不落库；由 MasterRuntime 的 SupervisorMemoryWriter 集中 staging。
    默认不注入 → Worker 产出空候选，行为与 P5 一致。
    """

    async def harvest(
        self, run: AgentRun, result: AgentRunResult
    ) -> tuple[MemoryCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class WorkerRun:
    step_id: str
    run: AgentRun
    result: AgentRunResult
    workspace: WorkspaceContext
    verification: VerificationResult


class AgentRuntime:
    def __init__(
        self,
        *,
        react_engine: ReActEngine,
        workspace_manager: WorkspaceManager,
        local_verifier: LocalVerifier | None = None,
        event_store: RawEventStore | None = None,
        candidate_harvester: WorkerCandidateHarvester | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._engine = react_engine
        self._wsm = workspace_manager
        self._verifier = local_verifier or AlwaysPassVerifier()
        self._events: RawEventStore = event_store or NullEventStore()
        self._harvester = candidate_harvester
        self._metrics = metrics or Metrics()

    async def run(
        self,
        definition: AgentDefinition,
        step: Step,
        *,
        session_id: str,
        cancellation: CancellationToken | None = None,
        trace_id: str | None = None,
    ) -> WorkerRun:
        run_id = new_agent_run_id()
        workspace = await self._wsm.create(run_id)
        run = AgentRun(
            definition=definition,
            session_id=session_id,
            workspace=workspace,
            run_id=run_id,
            _event_store=self._events,
        )
        run.context.trace_id = trace_id  # master_run_id → agent_run_id 溯源链（§27）
        if cancellation is not None:
            run.cancellation = cancellation

        timeout = definition.context_profile.agent_run_timeout_seconds
        try:
            async with asyncio.timeout(timeout):
                result = await self._engine.run_turn(run, step.instruction)
                verification = await self._verifier.verify(run, result)

                while (
                    not verification.ok
                    and run.reflection_count < definition.max_reflection_count
                ):
                    run.reflection_count += 1
                    self._metrics.incr("agent.reflection")
                    feedback = verification.feedback or "上一次未达成目标，请修正后重试。"
                    result = await self._engine.run_turn(run, f"[验证反馈] {feedback}")
                    verification = await self._verifier.verify(run, result)
        except TimeoutError:
            self._metrics.incr("agent.timeouts")
            run.status = RunStatus.FAILED
            result = AgentRunResult.failed(run.run_id, f"AgentRun 超时 ({timeout}s)")
            verification = VerificationResult(ok=False, reason="timeout")
        except Exception as exc:  # 保守失败：Worker 异常不打断整个编排。
            self._metrics.incr("agent.run_failures")
            run.status = RunStatus.FAILED
            result = AgentRunResult.failed(run.run_id, f"{type(exc).__name__}: {exc}")
            verification = VerificationResult(ok=False, reason="exception")

        if self._harvester is not None and result.ok:
            try:
                candidates = await self._harvester.harvest(run, result)
            except Exception:  # 保守失败：抽取候选出错不影响 Worker 结果。
                self._metrics.incr("agent.candidate_harvest_failures")
            else:
                if candidates:
                    result = replace(result, memory_candidates=candidates)

        return WorkerRun(
            step_id=step.id,
            run=run,
            result=result,
            workspace=workspace,
            verification=verification,
        )
