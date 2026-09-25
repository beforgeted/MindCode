from __future__ import annotations

from pathlib import Path

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.run import AgentRun
from codeagent.llm.stub_client import StubLlmClient
from codeagent.llm.types import ModelConfig
from codeagent.orchestration.global_verifier import LlmGlobalVerifier
from codeagent.orchestration.planner import LlmPlanner
from codeagent.orchestration.step_scheduler import SchedulerResult
from codeagent.orchestration.task_graph import TaskGraph
from codeagent.runtime.local_verifier import LlmLocalVerifier
from codeagent.workspace.context import WorkspaceContext

_MC = ModelConfig(model="stub", context_window=1000)
_DEFN = AgentDefinition(id="default", name="D", system_prompt="")


async def test_llm_planner_parses_graph():
    client = StubLlmClient(
        [
            '{"steps":[{"id":"a","instruction":"改 A"},'
            '{"id":"b","instruction":"改 B","dependencies":["a"]}]}'
        ]
    )
    graph = await LlmPlanner(client, _MC).plan("任务")
    assert {s.id for s in graph.steps} == {"a", "b"}
    assert graph.get("b").dependencies == frozenset({"a"})


async def test_llm_planner_degrades_to_single_step_on_garbage():
    client = StubLlmClient(["这不是 JSON", "还是不是 JSON"])
    graph = await LlmPlanner(client, _MC, max_repair_retries=1).plan("干点活")
    assert len(graph.steps) == 1
    assert graph.steps[0].instruction == "干点活"


async def test_llm_local_verifier_parses_verdict():
    client = StubLlmClient(['{"ok":false,"reason":"缺测试","feedback":"补测试"}'])
    run = AgentRun.create(_DEFN, session_id="s", workspace=WorkspaceContext.local(Path(".")))
    result = AgentRunResult.success(run.run_id, "改完了")
    verdict = await LlmLocalVerifier(client, _MC).verify(run, result)
    assert verdict.ok is False
    assert verdict.feedback == "补测试"


async def test_llm_local_verifier_conservative_on_llm_failure():
    class BoomClient:
        async def chat(self, *a, **k):
            raise RuntimeError("llm down")

        async def count_tokens(self, *a, **k):
            return None

    run = AgentRun.create(_DEFN, session_id="s", workspace=WorkspaceContext.local(Path(".")))
    result = AgentRunResult.success(run.run_id, "x")
    verdict = await LlmLocalVerifier(BoomClient(), _MC).verify(run, result)
    assert verdict.ok is True  # 保守失败：验证器故障不卡编排


async def test_llm_global_verifier_parses_verdict():
    client = StubLlmClient(['{"accept":false,"replan_instruction":"重做 b"}'])
    verdict = await LlmGlobalVerifier(client, _MC).verify("任务", TaskGraph([]), SchedulerResult())
    assert verdict.accept is False
    assert verdict.replan_instruction == "重做 b"
