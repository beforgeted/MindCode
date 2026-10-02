"""MasterSession：Multi-Agent 编排的 composition root（P5）。

复用单 Agent 的共享装配（event/artifact/memory/context/execution/engine），
在其上叠 WorkspaceManager(auto) / AgentRegistry / Planner / AgentRuntime /
StepScheduler / GlobalVerifier / MasterRuntime。

ReActEngine 本就是 run-agnostic（run_turn 接任意 AgentRun），所以多个并行 Worker
共享同一个 engine 实例，各自带独立 AgentRun + WorkspaceContext。

`build_master` 是唯一的装配入口，REPL 的 /task 直接复用**当前活着的** AgentSession，
不再另起一个 session。
"""

from __future__ import annotations

import hashlib
from types import TracebackType

from codeagent.agent.models import AgentDefinition
from codeagent.agent.registry import AgentRegistry
from codeagent.config import AppConfig
from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.evidence.event_store import RawEventStore
from codeagent.execution.models import SandboxUnavailable
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.infra.metrics import Metrics
from codeagent.llm.client import LlmClient
from codeagent.llm.observed_client import RoleLlmClient
from codeagent.llm.routing import attach_routing
from codeagent.llm.types import ModelConfig
from codeagent.memory.governance_repository import MemoryGovernanceRepository
from codeagent.observability import JsonTrajectoryExporter
from codeagent.orchestration.cost_store import CostStore
from codeagent.orchestration.global_verifier import (
    GlobalVerifier,
    LlmGlobalVerifier,
    NoFailureVerifier,
)
from codeagent.orchestration.integration_coordinator import IntegrationCoordinator
from codeagent.orchestration.integrator import InstructionIntegrator
from codeagent.orchestration.master_runtime import FinalResult, MasterRuntime
from codeagent.orchestration.planner import LlmPlanner, Planner
from codeagent.orchestration.run_store import RunStore, SqliteRunStore
from codeagent.orchestration.shared_memory import (
    NullSupervisorMemoryWriter,
    SupervisorMemoryWriter,
    SupervisorWriter,
)
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.worker_harvester import EventWorkerHarvester
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import LlmLocalVerifier, LocalVerifier, StatusLocalVerifier
from codeagent.runtime.react_engine import ReActEngine
from codeagent.session import AgentSession
from codeagent.tool.approval import DenyExternalApprovalPolicy, InteractiveApprovalPolicy
from codeagent.workspace.git_worktree import GitWorktreeWorkspaceManager
from codeagent.workspace.manager import _is_git_worktree, build_workspace_manager
from codeagent.workspace.snapshot import SnapshotWorkspaceManager


async def build_master(
    *,
    config: AppConfig,
    llm_client: LlmClient,
    engine: ReActEngine,
    event_store: RawEventStore,
    metrics: Metrics,
    definition: AgentDefinition,
    isolation: str = "auto",
    planner: Planner | None = None,
    local_verifier: LocalVerifier | None = None,
    global_verifier: GlobalVerifier | None = None,
    memory_store: MemoryGovernanceRepository | None = None,
    run_store: RunStore | None = None,
    artifact_store: ArtifactStore | None = None,
) -> MasterRuntime:
    """装配 MasterRuntime。stub LLM 下 Verifier 用确定性实现，真实模型下用 LLM 实现。"""
    if config.costs.prices and run_store is not None:
        if (not isinstance(run_store, SqliteRunStore) or
                run_store._path != (config.state_root / 'runs.db').resolve()):
            raise ValueError('成本路由要求与默认RunStore相同的SQLite路径')
    llm_client = attach_routing(
        llm_client, config.models, metrics=metrics, events=event_store, session_id="",
        costs=config.costs, cost_store=CostStore(config.state_root / 'runs.db'),
        capabilities=config.capabilities if config.capabilities.models else None,
        calibration=config.calibration,
    )
    sandbox = None
    if config.execution_backend == "podman":
        assert config.sandbox_image is not None
        sandbox = PodmanSandboxManager(
            config.sandbox_image, limits=config.sandbox_limits,
            ledger_directory=config.state_root / "sandbox" /
            hashlib.sha256(config.effective_project_id.encode()).hexdigest()[:24],
            project_id=config.effective_project_id,
        )
        await sandbox.ensure_available()
    try:
        wsm = await build_workspace_manager(config.workspace_root, isolation=isolation)
        if sandbox is not None and not isinstance(wsm, GitWorktreeWorkspaceManager):
            if _is_git_worktree(config.workspace_root):
                raise SandboxUnavailable("Podman Git /task requires worktree isolation")
            wsm = SnapshotWorkspaceManager(
                config.workspace_root, config.state_root, limits=sandbox.snapshot_limits,
            )
        if run_store is None:
            run_store = SqliteRunStore(config.state_root / "runs.db")
            await run_store.start()
        model_config = ModelConfig(
            model=config.model, context_window=config.profile.context_window
        )
        registry = AgentRegistry(default=definition)
        stub = config.use_stub_llm
        lverif = local_verifier or (
            StatusLocalVerifier() if stub else LlmLocalVerifier(
                RoleLlmClient(llm_client, "local_verifier"), model_config,
            )
        )
        gverif = global_verifier or (
            NoFailureVerifier() if stub else LlmGlobalVerifier(
                RoleLlmClient(llm_client, "global_verifier"), model_config,
                limits=config.verification,
            )
        )
        runtime = AgentRuntime(
            react_engine=engine,
            workspace_manager=wsm,
            local_verifier=lverif,
            event_store=event_store,
            metrics=metrics,
            sandbox_manager=sandbox,
            candidate_harvester=(
                EventWorkerHarvester(event_store, config.effective_project_id)
                if memory_store is not None
                else None
            ),
        )
        scheduler = StepScheduler(
            agent_runtime=runtime,
            agent_registry=registry,
            max_concurrency=config.profile.agent_max_concurrency,
            isolated=wsm.isolated,
            integration_coordinator=IntegrationCoordinator(wsm, metrics=metrics),
            max_reruns=config.profile.agent_max_reruns,
            integrator=InstructionIntegrator(),
            max_integrations=config.profile.agent_max_integrations,
            metrics=metrics,
        )
        memory_writer: SupervisorWriter = (
            SupervisorMemoryWriter(memory_store, metrics=metrics)
            if memory_store is not None
            else NullSupervisorMemoryWriter()
        )
        return MasterRuntime(
            planner=planner or LlmPlanner(RoleLlmClient(llm_client, "planner"), model_config),
            scheduler=scheduler,
            global_verifier=gverif,
            workspace_manager=wsm,
            max_replans=config.profile.master_max_replans,
            promote_max_retries=config.profile.promote_max_retries,
            verify_command=config.verify_command,
            sandbox_manager=sandbox,
            memory_writer=memory_writer,
            run_store=run_store,
            cost_store=CostStore(config.state_root / 'runs.db') if config.costs.prices else None,
            verification_limits=config.verification,
            metrics=metrics,
            approval_policy=(
                InteractiveApprovalPolicy()
                if config.interactive_approval and sandbox is None
                else DenyExternalApprovalPolicy()
            ),
            artifact_store=artifact_store,
            trajectory_exporter=JsonTrajectoryExporter(config.state_root, run_store, event_store),
        )
    except BaseException:
        if sandbox is not None:
            await sandbox.aclose()
        raise


class MasterSession:
    """独立/编程使用：自持一个 AgentSession 生命周期。REPL 走 build_master 复用活 session。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        llm_client: LlmClient,
        isolation: str = "auto",
        planner: Planner | None = None,
        local_verifier: LocalVerifier | None = None,
        global_verifier: GlobalVerifier | None = None,
    ) -> None:
        self._config = config
        self._llm = llm_client
        self._isolation = isolation
        self._planner = planner
        self._local_verifier = local_verifier
        self._global_verifier = global_verifier
        self.session = AgentSession(config, llm_client=llm_client)
        self.master: MasterRuntime | None = None

    async def __aenter__(self) -> MasterSession:
        await self.session.__aenter__()
        try:
            self.master = await build_master(
                config=self._config,
                llm_client=self.session.llm_client,
                engine=self.session.engine,
                event_store=self.session.event_store,
                metrics=self.session.metrics,
                definition=self.session.definition,
                isolation=self._isolation,
                planner=self._planner,
                local_verifier=self._local_verifier,
                global_verifier=self._global_verifier,
                artifact_store=self.session.artifact_store,
            )
        except BaseException as exc:
            await self.session.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    async def run_task(self, task: str) -> FinalResult:
        assert self.master is not None, "MasterSession 未进入上下文"
        return await self.master.run(task, session_id=self.session.session_id)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self.master is not None:
                await self.master.aclose()
        finally:
            await self.session.__aexit__(exc_type, exc, tb)
