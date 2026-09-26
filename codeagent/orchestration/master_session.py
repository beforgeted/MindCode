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

from types import TracebackType

from codeagent.agent.models import AgentDefinition
from codeagent.agent.registry import AgentRegistry
from codeagent.config import AppConfig
from codeagent.evidence.event_store import RawEventStore
from codeagent.infra.metrics import Metrics
from codeagent.llm.client import LlmClient
from codeagent.llm.types import ModelConfig
from codeagent.memory.governance_repository import MemoryGovernanceRepository
from codeagent.orchestration.global_verifier import (
    GlobalVerifier,
    LlmGlobalVerifier,
    NoFailureVerifier,
)
from codeagent.orchestration.master_runtime import FinalResult, MasterRuntime
from codeagent.orchestration.planner import LlmPlanner, Planner
from codeagent.orchestration.run_store import RunStore
from codeagent.orchestration.shared_memory import (
    NullSupervisorMemoryWriter,
    SupervisorMemoryWriter,
    SupervisorWriter,
)
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import LlmLocalVerifier, LocalVerifier, StatusLocalVerifier
from codeagent.runtime.react_engine import ReActEngine
from codeagent.session import AgentSession
from codeagent.workspace.manager import build_workspace_manager


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
) -> MasterRuntime:
    """装配 MasterRuntime。stub LLM 下 Verifier 用确定性实现，真实模型下用 LLM 实现。"""
    model_config = ModelConfig(
        model=config.model, context_window=config.profile.context_window
    )
    wsm = await build_workspace_manager(config.workspace_root, isolation=isolation)
    registry = AgentRegistry(default=definition)
    stub = config.use_stub_llm
    lverif = local_verifier or (
        StatusLocalVerifier() if stub else LlmLocalVerifier(llm_client, model_config)
    )
    gverif = global_verifier or (
        NoFailureVerifier() if stub else LlmGlobalVerifier(llm_client, model_config)
    )
    runtime = AgentRuntime(
        react_engine=engine,
        workspace_manager=wsm,
        local_verifier=lverif,
        event_store=event_store,
        metrics=metrics,
    )
    scheduler = StepScheduler(
        agent_runtime=runtime,
        agent_registry=registry,
        max_concurrency=config.profile.agent_max_concurrency,
        isolated=wsm.isolated,
        metrics=metrics,
    )
    memory_writer: SupervisorWriter = (
        SupervisorMemoryWriter(memory_store, metrics=metrics)
        if memory_store is not None
        else NullSupervisorMemoryWriter()
    )
    return MasterRuntime(
        planner=planner or LlmPlanner(llm_client, model_config),
        scheduler=scheduler,
        global_verifier=gverif,
        workspace_manager=wsm,
        max_replans=config.profile.master_max_replans,
        memory_writer=memory_writer,
        run_store=run_store,
        metrics=metrics,
    )


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
        self.master = await build_master(
            config=self._config,
            llm_client=self._llm,
            engine=self.session.engine,
            event_store=self.session.event_store,
            metrics=self.session.metrics,
            definition=self.session.definition,
            isolation=self._isolation,
            planner=self._planner,
            local_verifier=self._local_verifier,
            global_verifier=self._global_verifier,
        )
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
        await self.session.__aexit__(exc_type, exc, tb)
