from __future__ import annotations

import asyncio
from pathlib import Path

from codeagent.agent.models import AgentDefinition, AgentRunResult
from codeagent.agent.registry import AgentRegistry
from codeagent.agent.run import AgentRun
from codeagent.infra.cancellation import CancellationToken
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.workspace.context import WorkspaceContext

_DEFN = AgentDefinition(id="default", name="D", system_prompt="")


class FakeRuntime:
    def __init__(self, *, delays=None, fail: set[str] | frozenset[str] = frozenset()):
        self._delays = delays or {}
        self._fail = fail
        self.active = 0
        self.peak = 0
        self.completion_order: list[str] = []

    async def run(
        self, definition, step, *, session_id, cancellation=None, trace_id=None
    ) -> WorkerRun:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._delays.get(step.id, 0))
            failed = step.id in self._fail
            run = AgentRun.create(
                definition, session_id=session_id, workspace=WorkspaceContext.local(Path("."))
            )
            result = (
                AgentRunResult.failed(run.run_id, "boom")
                if failed
                else AgentRunResult.success(run.run_id, step.id)
            )
            self.completion_order.append(step.id)
            return WorkerRun(
                step_id=step.id,
                run=run,
                result=result,
                workspace=run.workspace,
                verification=VerificationResult(ok=not failed),
            )
        finally:
            self.active -= 1


def _scheduler(runtime, *, max_concurrency=2, isolated=True) -> StepScheduler:
    return StepScheduler(
        agent_runtime=runtime,
        agent_registry=AgentRegistry(default=_DEFN),
        max_concurrency=max_concurrency,
        isolated=isolated,
    )


async def test_independent_steps_run_in_parallel():
    runtime = FakeRuntime(delays={"a": 0.05, "b": 0.05})
    graph = TaskGraph([Step("a", "default", "A"), Step("b", "default", "B")])
    result = await _scheduler(runtime).run(graph, session_id="s")
    assert result.completed == {"a", "b"}
    assert runtime.peak == 2


async def test_pending_set_no_barrier_between_branches():
    # a → b(慢), a → c(快) → d。d 应在慢的 b 之前完成（无波次 barrier）。
    runtime = FakeRuntime(delays={"b": 0.2, "c": 0.01, "d": 0.01})
    graph = TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
            Step("c", "default", "C", dependencies=frozenset({"a"})),
            Step("d", "default", "D", dependencies=frozenset({"c"})),
        ]
    )
    result = await _scheduler(runtime, max_concurrency=4).run(graph, session_id="s")
    assert result.completed == {"a", "b", "c", "d"}
    assert runtime.completion_order.index("d") < runtime.completion_order.index("b")


async def test_failure_blocks_dependents():
    runtime = FakeRuntime(fail={"a"})
    graph = TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
            Step("c", "default", "C"),
        ]
    )
    result = await _scheduler(runtime).run(graph, session_id="s")
    assert result.failed == {"a"}
    assert result.completed == {"c"}
    assert result.blocked == {"b"}
    assert "b" not in runtime.completion_order  # 从未派发


async def test_non_isolated_serializes_writes_but_parallelizes_reads():
    graph_writes = TaskGraph(
        [Step("a", "default", "A"), Step("b", "default", "B")]  # 默认 read_only=False
    )
    writer = FakeRuntime(delays={"a": 0.05, "b": 0.05})
    await _scheduler(writer, isolated=False).run(graph_writes, session_id="s")
    assert writer.peak == 1  # 写操作被写锁串行

    graph_reads = TaskGraph(
        [
            Step("a", "default", "A", read_only=True),
            Step("b", "default", "B", read_only=True),
        ]
    )
    reader = FakeRuntime(delays={"a": 0.05, "b": 0.05})
    await _scheduler(reader, isolated=False).run(graph_reads, session_id="s")
    assert reader.peak == 2  # 只读免锁，仍并行


async def test_cancellation_token_is_forwarded_to_workers():
    seen: list = []

    class RecordingRuntime:
        async def run(self, definition, step, *, session_id, cancellation=None, trace_id=None):
            seen.append(cancellation)
            run = AgentRun.create(
                definition, session_id=session_id, workspace=WorkspaceContext.local(Path("."))
            )
            return WorkerRun(
                step_id=step.id,
                run=run,
                result=AgentRunResult.success(run.run_id, step.id),
                workspace=run.workspace,
                verification=VerificationResult(ok=True),
            )

    token = CancellationToken()
    graph = TaskGraph([Step("a", "default", "A")])
    await _scheduler(RecordingRuntime()).run(graph, session_id="s", cancellation=token)
    assert seen == [token]
