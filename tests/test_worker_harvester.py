"""Phase 0 C7：Worker 候选抽取器接线 + promote 成功才 stage。"""

from __future__ import annotations

from pathlib import Path

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.evidence.jsonl_event_store import JsonlEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.memory.models import MemorySource
from codeagent.orchestration.worker_harvester import EventWorkerHarvester
from codeagent.workspace.context import WorkspaceContext

_DEFN = AgentDefinition(id="default", name="D", system_prompt="")


async def test_harvester_filters_by_run_and_extracts(tmp_path: Path):
    store = JsonlEventStore(tmp_path)
    await store.start()
    try:
        store.append_nowait(AgentEvent(
            type=EventType.ASSISTANT_MESSAGE, session_id="s", agent_run_id="A",
            payload={"text": "决定用方案 X"},
        ))
        store.append_nowait(AgentEvent(
            type=EventType.ASSISTANT_MESSAGE, session_id="s", agent_run_id="B",
            payload={"text": "别的 run 的话"},
        ))
        run = AgentRun.create(_DEFN, session_id="s", workspace=WorkspaceContext.local(tmp_path))
        run.run_id = "A"
        candidates = await EventWorkerHarvester(store, "proj").harvest(
            run, AgentRunResult.success("A", "done")
        )
    finally:
        await store.aclose()

    assert len(candidates) == 1  # 只抽本 run（A）的事件
    assert candidates[0].content == "决定用方案 X"
    assert candidates[0].source is MemorySource.ASSISTANT_DERIVED
    assert candidates[0].evidence_refs[0].agent_run_id == "A"  # 可溯源
