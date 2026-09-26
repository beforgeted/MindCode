from __future__ import annotations

from pathlib import Path

from codeagent.agent.models import AgentDefinition, AgentRunResult, FileChangeKind, FileState
from codeagent.agent.registry import AgentRegistry
from codeagent.agent.run import AgentRun
from codeagent.orchestration.global_verifier import NoFailureVerifier
from codeagent.orchestration.master_runtime import MasterRuntime
from codeagent.orchestration.planner import StaticPlanner
from codeagent.orchestration.run_store import (
    NullRunStore,
    SqliteRunStore,
    StepOutcome,
    graph_from_json,
    graph_to_json,
)
from codeagent.orchestration.step_scheduler import StepScheduler
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import WorkerRun
from codeagent.runtime.local_verifier import VerificationResult
from codeagent.workspace.context import WorkspaceContext
from codeagent.workspace.manager import LocalWorkspaceManager

_DEFN = AgentDefinition(id="default", name="D", system_prompt="")


class CountingRuntime:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def run(
        self, definition, step, *, session_id, cancellation=None, trace_id=None
    ) -> WorkerRun:
        self.ran.append(step.id)
        run = AgentRun.create(
            definition, session_id=session_id, workspace=WorkspaceContext.local(Path("."))
        )
        result = AgentRunResult.success(
            run.run_id, step.id, files=(FileState(f"{step.id}.py", FileChangeKind.CREATED),)
        )
        return WorkerRun(
            step_id=step.id,
            run=run,
            result=result,
            workspace=run.workspace,
            verification=VerificationResult(ok=True),
        )


def _master(graph, runtime, store, *, tmp: Path) -> MasterRuntime:
    scheduler = StepScheduler(
        agent_runtime=runtime,  # type: ignore[arg-type]
        agent_registry=AgentRegistry(default=_DEFN),
        max_concurrency=2,
        isolated=False,
    )
    return MasterRuntime(
        planner=StaticPlanner(graph),
        scheduler=scheduler,
        global_verifier=NoFailureVerifier(),
        workspace_manager=LocalWorkspaceManager(tmp),
        run_store=store,
    )


def _graph() -> TaskGraph:
    return TaskGraph(
        [
            Step("a", "default", "A"),
            Step("b", "default", "B", dependencies=frozenset({"a"})),
        ]
    )


async def test_graph_json_round_trip():
    graph = _graph()
    restored = graph_from_json(graph_to_json(graph))
    assert {s.id for s in restored.steps} == {"a", "b"}
    assert restored.get("b").dependencies == frozenset({"a"})


async def test_run_store_persists_and_loads(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "runs.db")
    await store.start()
    graph = _graph()
    await store.save_run(
        master_run_id="mrun_1", session_id="s", task="做 A 再做 B", graph=graph, status="running"
    )
    await store.record_step(
        "mrun_1",
        StepOutcome(
            step_id="a",
            status="integrated",
            summary="done a",
            files=(FileState("a.py", FileChangeKind.CREATED),),
            branch_name="w/a",
        ),
    )
    record = await store.load_run("mrun_1")
    assert record is not None
    assert record.task == "做 A 再做 B"
    assert {s.id for s in record.graph.steps} == {"a", "b"}
    assert record.outcomes["a"].integrated
    assert record.outcomes["a"].files[0].path == "a.py"
    assert await store.load_run("missing") is None


async def test_null_run_store_is_noop():
    store = NullRunStore()
    await store.save_run(
        master_run_id="x", session_id="s", task="t", graph=_graph(), status="running"
    )
    assert await store.load_run("x") is None


async def test_resume_skips_completed_steps(tmp_path: Path):
    store = SqliteRunStore(tmp_path / "runs.db")
    await store.start()
    graph = _graph()
    # 模拟上次跑到一半崩溃：graph 已落库，step a 已完成。
    await store.save_run(
        master_run_id="mrun_x", session_id="s", task="做 A 再做 B", graph=graph, status="running"
    )
    await store.record_step(
        "mrun_x",
        StepOutcome(
            step_id="a",
            status="integrated",
            summary="a",
            files=(FileState("a.py", FileChangeKind.CREATED),),
        ),
    )

    runtime = CountingRuntime()
    master = _master(graph, runtime, store, tmp=tmp_path)
    final = await master.run("做 A 再做 B", session_id="s", resume_master_run_id="mrun_x")

    assert final.accepted
    assert runtime.ran == ["b"]  # a 被跳过，只重跑 b
    assert final.scheduler is not None
    assert final.scheduler.completed == {"a", "b"}
    # 聚合含跳过的 a（来自持久化）与新跑的 b。
    assert {f.path for f in final.files} == {"a.py", "b.py"}
    record = await store.load_run("mrun_x")
    assert record is not None and record.status == "success"
