from __future__ import annotations

from pathlib import Path
from typing import cast

from codeagent.agent.models import AgentDefinition, AgentRunResult, FileChangeKind, FileState
from codeagent.agent.registry import AgentRegistry
from codeagent.agent.run import AgentRun
from codeagent.orchestration.global_verifier import GlobalVerdict, NoFailureVerifier
from codeagent.orchestration.master_runtime import MasterRuntime
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.manager import LocalWorkspaceManager

_DEFN = AgentDefinition(id="default", name="D", system_prompt="")


class FakeRuntime:
    def __init__(self, *, fail: set[str] | frozenset[str] = frozenset()):
        self._fail = fail
        self.trace_ids: list = []

    async def run(
        self, definition, step, *, session_id, cancellation=None, trace_id=None
    ) -> WorkerRun:
        self.trace_ids.append(trace_id)
        run = AgentRun.create(
            definition, session_id=session_id, workspace=WorkspaceContext.local(Path("."))
        )
        failed = step.id in self._fail
        if failed:
            result = AgentRunResult.failed(run.run_id, "boom")
        else:
            result = AgentRunResult.success(
                run.run_id, step.id, files=(FileState(f"{step.id}.py", FileChangeKind.CREATED),)
            )
        return WorkerRun(
            step_id=step.id,
            run=run,
            result=result,
            workspace=run.workspace,
            verification=VerificationResult(ok=not failed),
        )


def _master(graph, runtime, verifier=None, *, max_replans=1, tmp: Path | str = "."):
    scheduler = StepScheduler(
        agent_runtime=runtime,
        agent_registry=AgentRegistry(default=_DEFN),
        max_concurrency=2,
        isolated=False,
    )
    return MasterRuntime(
        planner=StaticPlanner(graph),
        scheduler=scheduler,
        global_verifier=verifier or NoFailureVerifier(),
        workspace_manager=LocalWorkspaceManager(tmp),
        max_replans=max_replans,
    )


async def test_master_runs_multi_step_and_aggregates(tmp_path: Path):
    graph = TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
        ]
    )
    master = _master(graph, FakeRuntime(), tmp=tmp_path)
    final = await master.run("做 A 再做 B", session_id="s")
    assert final.accepted
    assert final.scheduler is not None
    assert final.scheduler.completed == {"a", "b"}
    assert {f.path for f in final.files} == {"a.py", "b.py"}
    assert final.replans == 0
    # master_run_id 生成并作为 trace_id 下传给每个 Worker（§27 溯源链）。
    assert final.master_run_id
    runtime = cast(FakeRuntime, master._scheduler._runtime)  # type: ignore[attr-defined]
    assert set(runtime.trace_ids) == {final.master_run_id}


async def test_master_replans_on_rejection(tmp_path: Path):
    graph = TaskGraph([Step("a", "default", "A")])

    class RejectOnce:
        def __init__(self):
            self.calls = 0

        async def verify(self, task, g, results) -> GlobalVerdict:
            self.calls += 1
            if self.calls == 1:
                return GlobalVerdict(accept=False, replan_instruction="重来")
            return GlobalVerdict(accept=True)

    master = _master(graph, FakeRuntime(), RejectOnce(), max_replans=1, tmp=tmp_path)
    final = await master.run("任务", session_id="s")
    assert final.accepted
    assert final.replans == 1


async def test_master_not_accepted_when_step_fails(tmp_path: Path):
    graph = TaskGraph([Step("a", "default", "A")])
    master = _master(graph, FakeRuntime(fail={"a"}), max_replans=0, tmp=tmp_path)
    final = await master.run("任务", session_id="s")
    assert not final.accepted
    assert final.scheduler is not None
    assert final.scheduler.failed == {"a"}
