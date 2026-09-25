from __future__ import annotations

from pathlib import Path

from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra.cancellation import CancellationToken
from codeagent.memory.models import MemoryScope, MemorySource, MemoryType, NewMemoryItem
from codeagent.memory.sqlite_store import SqliteMemoryStore
from codeagent.tool.builtin.evidence_get import EvidenceGetTool
from codeagent.tool.builtin.memory_get import MemoryGetTool
from codeagent.tool.execution_manager import ExecutionScope, ToolExecutionManager
from codeagent.tool.models import ToolCall, ToolResultStatus
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolRegistry
from codeagent.workspace.context import WorkspaceContext


def _manager(registry, artifact_store) -> ToolExecutionManager:
    estimator = HeuristicTokenEstimator()
    return ToolExecutionManager(
        registry=registry,
        normalizer=ToolResultNormalizer(estimator=estimator, artifact_store=artifact_store),
        artifact_store=artifact_store,
    )


def _scope(workspace, profile, session_id="ses_1") -> ExecutionScope:
    return ExecutionScope(
        agent_run_id="run_1",
        session_id=session_id,
        workspace=WorkspaceContext.local(workspace),
        cancellation=CancellationToken(),
        profile=profile,
        turn_id="turn_1",
    )


async def test_memory_get_returns_full_item(tmp_path: Path, workspace, profile, artifact_store):
    store = SqliteMemoryStore(tmp_path / "memory.db")
    await store.start()
    try:
        item = await store.create(
            NewMemoryItem(
                project_id="p",
                scope=MemoryScope.PROJECT,
                scope_id="p",
                type=MemoryType.CONSTRAINT,
                content="项目固定使用 Python 3.11",
                source=MemorySource.USER_EXPLICIT,
            )
        )
        registry = ToolRegistry([MemoryGetTool(store, "p")])
        manager = _manager(registry, artifact_store)
        outcome = await manager.execute_batch(
            _scope(workspace, profile),
            [ToolCall("tu_1", "memory_get", {"memory_id": item.id})],
        )
        result = outcome.results[0]
        assert result.status is ToolResultStatus.OK
        assert "Python 3.11" in result.content
        assert item.id in result.content

        missing = await manager.execute_batch(
            _scope(workspace, profile),
            [ToolCall("tu_2", "memory_get", {"memory_id": "mem_nope"})],
        )
        assert missing.results[0].status is ToolResultStatus.ERROR
    finally:
        await store.aclose()


async def test_evidence_get_reads_raw_event(tmp_path: Path, workspace, profile, artifact_store):
    events = JsonlEventStore(tmp_path)
    await events.start()
    try:
        event = AgentEvent(
            type=EventType.USER_MESSAGE, session_id="ses_1", payload={"text": "证据正文"}
        )
        events.append_nowait(event)
        await events.flush()

        registry = ToolRegistry([EvidenceGetTool(events)])
        manager = _manager(registry, artifact_store)
        outcome = await manager.execute_batch(
            _scope(workspace, profile),
            [ToolCall("tu_1", "evidence_get", {"event_id": event.event_id})],
        )
        result = outcome.results[0]
        assert result.status is ToolResultStatus.OK
        assert "证据正文" in result.content
    finally:
        await events.aclose()
