"""MasterSession：Multi-Agent 编排的 composition root（P5）。

复用单 Agent 的共享装配（event/artifact/memory/context/execution/engine），
在其上叠 WorkspaceManager(auto) / AgentRegistry / Planner / AgentRuntime /
StepScheduler / GlobalVerifier / MasterRuntime。

ReActEngine 本就是 run-agnostic（run_turn 接任意 AgentRun），所以多个并行 Worker
共享同一个 engine 实例，各自带独立 AgentRun + WorkspaceContext。
"""

from __future__ import annotations

from types import TracebackType

from codeagent.agent.registry import AgentRegistry
from codeagent.config import AppConfig
from codeagent.llm.client import LlmClient
from codeagent.llm.types import ModelConfig
from codeagent.orchestration.global_verifier import GlobalVerifier, NoFailureVerifier
from codeagent.orchestration.master_runtime import FinalResult, MasterRuntime
from codeagent.orchestration.planner import LlmPlanner, Planner
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.runtime.agent_runtime import AgentRuntime
from codeagent.runtime.local_verifier import LocalVerifier, StatusLocalVerifier
from codeagent.session import AgentSession
from codeagent.workspace.manager import build_workspace_manager


class MasterSession:
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
        self._planner_override = planner
        self._local_verifier = local_verifier
        self._global_verifier = global_verifier
        self.session = AgentSession(config, llm_client=llm_client)
        self.master: MasterRuntime | None = None

    async def __aenter__(self) -> MasterSession:
        await self.session.__aenter__()
        model_config = ModelConfig(
            model=self._config.model, context_window=self._config.profile.context_window
        )
        wsm = await build_workspace_manager(
            self._config.workspace_root, isolation=self._isolation
        )
        registry = AgentRegistry(default=self.session.definition)
        agent_runtime = AgentRuntime(
            react_engine=self.session.engine,
            workspace_manager=wsm,
            local_verifier=self._local_verifier or StatusLocalVerifier(),
            event_store=self.session.event_store,
            metrics=self.session.metrics,
        )
        scheduler = StepScheduler(
            agent_runtime=agent_runtime,
            agent_registry=registry,
            max_concurrency=self._config.profile.agent_max_concurrency,
            isolated=wsm.isolated,
            metrics=self.session.metrics,
        )
        planner = self._planner_override or LlmPlanner(self._llm, model_config)
        self.master = MasterRuntime(
            planner=planner,
            scheduler=scheduler,
            global_verifier=self._global_verifier or NoFailureVerifier(),
            workspace_manager=wsm,
            max_replans=self._config.profile.master_max_replans,
            metrics=self.session.metrics,
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
