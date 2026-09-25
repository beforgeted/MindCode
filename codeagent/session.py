"""单 Agent 会话的 composition root。

P0-P3 的 Evidence、工具治理、History Compaction 与 PROJECT Durable Memory
都在这里装配；ReActEngine 只消费 ContextManager.prepare()，不感知这些策略。
"""

from __future__ import annotations

from types import TracebackType

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.config import DEFAULT_SYSTEM_PROMPT, AppConfig
from codeagent.context.compact.base import HistoryCompactor
from codeagent.context.compact.history_compactor import ConversationHistoryCompactor
from codeagent.context.manager import ContextManager, ContextPreparationResult
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.infra.ids import new_session_id
from codeagent.infra.metrics import Metrics
from codeagent.llm.client import LlmClient
from codeagent.llm.types import ModelConfig
from codeagent.memory.dedup import MemoryDeduplicator
from codeagent.memory.governance_service import MemoryGovernanceService
from codeagent.memory.index_projector import MemoryIndexProjector
from codeagent.memory.judge import LlmMemoryJudge
from codeagent.memory.models import MemorySource
from codeagent.memory.retriever import KeywordMemoryRetriever
from codeagent.memory.service import MemoryService
from codeagent.memory.sqlite_store import SqliteMemoryStore
from codeagent.runtime.react_engine import ReActEngine
from codeagent.tool.builtin import default_tools
from codeagent.tool.builtin.evidence_get import EvidenceGetTool
from codeagent.tool.builtin.memory_get import MemoryGetTool
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

        self.event_store = JsonlEventStore(config.state_root)
        self.artifact_store = FileArtifactStore(config.state_root)
        self.memory_store = SqliteMemoryStore(config.state_root / "memory.db")
        self.memory_projector = MemoryIndexProjector(
            config.state_root,
            self.memory_store,
            config.effective_project_id,
        )
        self.memory_service = MemoryService(
            self.memory_store,
            self.memory_projector,
            self.event_store,
            project_id=config.effective_project_id,
            session_id=self.session_id,
        )
        self.memory_retriever = KeywordMemoryRetriever(
            self.memory_store,
            config.effective_project_id,
            source_weights={
                MemorySource.USER_EXPLICIT: config.profile.memory_weight_user_explicit,
                MemorySource.TOOL_VERIFIED: config.profile.memory_weight_tool_verified,
                MemorySource.ASSISTANT_DERIVED: config.profile.memory_weight_assistant_derived,
            },
            importance_weight=config.profile.memory_importance_weight,
        )
        self.governance = MemoryGovernanceService(
            event_store=self.event_store,
            repository=self.memory_store,
            judge=LlmMemoryJudge(
                llm_client,
                ModelConfig(
                    model=config.model, context_window=config.profile.context_window
                ),
                max_repair_retries=config.profile.memory_judge_max_retries,
            ),
            project_id=config.effective_project_id,
            batch_limit=config.profile.memory_harvest_batch_limit,
            promote_limit=config.profile.memory_promote_limit,
            prefilter_max_bytes=config.profile.memory_prefilter_max_bytes,
            deduplicator=MemoryDeduplicator(
                jaccard_threshold=config.profile.memory_dedup_jaccard
            ),
            metrics=self.metrics,
        )
        self.estimator = HeuristicTokenEstimator()
        self.registry = ToolRegistry(
            [
                *default_tools(),
                MemoryGetTool(self.memory_store, config.effective_project_id),
                EvidenceGetTool(self.event_store),
            ]
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
        active_compactor = compactor or ConversationHistoryCompactor(
            llm_client,
            self.estimator,
            self.definition.model_config,
            metrics=self.metrics,
        )

        self.context_manager = ContextManager(
            estimator=self.estimator,
            compactor=active_compactor,
            memory_retriever=self.memory_retriever,
            metrics=self.metrics,
        )
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
        startup = await self.memory_service.start()
        if not self.memory_service.available:
            from codeagent.memory.retriever import NullMemoryRetriever

            self.context_manager.memory_retriever = NullMemoryRetriever()
        if startup.warning:
            self.metrics.incr("memory.index.warnings")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self.memory_service.available:
            await self.run_governance()
            await self.memory_service.aclose()
        await self.event_store.aclose()

    async def run_governance(self) -> str:
        """Session End 记忆治理（记忆 V2 §44）：抽取 → Judge → 去重/冲突 → 落库 → 刷新索引。

        保守失败：治理链内部异常不会打断关闭流程。
        """
        if not self.memory_service.available:
            return "[memory 不可用，跳过治理]"
        try:
            await self.event_store.flush()
            harvest, promote = await self.governance.run(self.session_id)
            await self.memory_projector.refresh_if_stale()
            return (
                f"[治理] 抽取 staged={harvest.staged} receipts={harvest.receipts} | "
                f"judged={promote.judged} promoted={promote.promoted} "
                f"superseded={promote.superseded} skipped={promote.skipped}"
            )
        except Exception as exc:
            self.metrics.incr("memory.governance.session_end_failures")
            return f"[治理失败，已跳过] {type(exc).__name__}: {exc}"

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
