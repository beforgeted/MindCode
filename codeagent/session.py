"""装配层：把 P0/P1 的所有组件接成一个可用的单 Agent 会话。

依赖方向自上而下，没有循环：

    AgentSession
      ├── JsonlEventStore / FileArtifactStore      (evidence)
      ├── ContextManager                           (context)
      │     ├── HeuristicTokenEstimator
      │     ├── ContextBudgetPredictor
      │     ├── ImagePayloadPruner
      │     ├── ToolResultOffloader
      │     └── NullCompactor                      <- P2 换成真 Compactor
      ├── ToolExecutionManager                     (tool)
      │     ├── ToolRegistry + builtin tools
      │     └── ToolResultNormalizer
      └── ReActEngine                              (runtime)

换 P2 只需要把 NullCompactor 换掉；接 P3 只需要给 ContextManager 加
MemoryRetriever。ReActEngine 和 AgentSession 都不用改。
"""

from __future__ import annotations

from types import TracebackType

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.config import DEFAULT_SYSTEM_PROMPT, AppConfig
from codeagent.context.compact.base import HistoryCompactor
from codeagent.context.manager import ContextManager, ContextPreparationResult
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.infra.ids import new_session_id
from codeagent.infra.metrics import Metrics
from codeagent.llm.client import LlmClient
from codeagent.llm.types import ModelConfig
from codeagent.runtime.react_engine import ReActEngine
from codeagent.tool.builtin import default_tools
from codeagent.tool.execution_manager import ToolExecutionManager
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolRegistry
from codeagent.workspace.context import WorkspaceContext


class AgentSession:
    def __init__(
        self,
        config: AppConfig,
        *,
        llm_client: LlmClient,
        definition: AgentDefinition | None = None,
        compactor: HistoryCompactor | None = None,
        session_id: str | None = None,
    ) -> None:
        self.config = config
        self.session_id = session_id or new_session_id()
        self.metrics = Metrics()

        self.event_store = JsonlEventStore(config.home)
        self.artifact_store = FileArtifactStore(config.home)
        self.estimator = HeuristicTokenEstimator()

        self.context_manager = ContextManager(
            estimator=self.estimator,
            compactor=compactor,
            metrics=self.metrics,
        )
        self.registry = ToolRegistry(default_tools())
        self.execution_manager = ToolExecutionManager(
            registry=self.registry,
            normalizer=ToolResultNormalizer(
                estimator=self.estimator,
                artifact_store=self.artifact_store,
                metrics=self.metrics,
            ),
            artifact_store=self.artifact_store,
            max_concurrency=config.max_tool_concurrency,
            event_store=self.event_store,
            metrics=self.metrics,
        )
        self.engine = ReActEngine(
            llm_client=llm_client,
            registry=self.registry,
            execution_manager=self.execution_manager,
            context_manager=self.context_manager,
            event_store=self.event_store,
            metrics=self.metrics,
        )

        self.definition = definition or AgentDefinition(
            id="mindcode",
            name="MindCode",
            system_prompt=DEFAULT_SYSTEM_PROMPT,
            model_config=ModelConfig(
                model=config.model, context_window=config.profile.context_window
            ),
            allowed_tools=self.registry.names(),
            context_profile=config.profile,
        )
        self.run = self._new_run()

    def _new_run(self) -> AgentRun:
        return AgentRun.create(
            self.definition,
            session_id=self.session_id,
            workspace=WorkspaceContext.local(self.config.workspace_root),
            event_store=self.event_store,
        )

    async def __aenter__(self) -> AgentSession:
        await self.event_store.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self.event_store.aclose()

    async def send(self, user_input: str) -> AgentRunResult:
        return await self.engine.run_turn(self.run, user_input)

    @property
    def last_prepared(self) -> ContextPreparationResult | None:
        prepared = self.run.context.last_prepared
        return prepared if isinstance(prepared, ContextPreparationResult) else None

    def clear(self) -> None:
        """`/clear`：结束当前 Runtime Context，开新的。

        Raw Events 保留，Durable Memory（P3）也不受影响。
        Clear Context != Forget Memory。
        """
        self.run = self._new_run()

    @property
    def profile(self):
        return self.definition.context_profile
