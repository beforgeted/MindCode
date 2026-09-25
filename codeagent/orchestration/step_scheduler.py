"""StepScheduler：pending-set 增量派发（V1 §3 裁决，不按波次/barrier）。

每完成一个 Step 就重算 ready 集合并立刻派发，长短分支不互相等待。
不用 asyncio.TaskGroup（首个异常会取消所有兄弟任务 → 孤立 tool_call）；
改用 asyncio.wait(FIRST_COMPLETED)。AgentRuntime.run 自身已保证不抛（保守失败）。

写串行回退：workspace 非隔离时，非 read_only 的 Step 全程持写锁串行执行；
read_only Step 免锁 → 仍可并行。隔离（worktree）时各 Worker 各自目录，无需写锁。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from codeagent.agent.registry import AgentRegistry
from codeagent.infra.cancellation import CancellationToken
from codeagent.infra.metrics import Metrics
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import AgentRuntime, WorkerRun


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    workers: dict[str, WorkerRun] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)
    failed: set[str] = field(default_factory=set)
    blocked: set[str] = field(default_factory=set)
    max_parallel: int = 0


class StepScheduler:
    def __init__(
        self,
        *,
        agent_runtime: AgentRuntime,
        agent_registry: AgentRegistry,
        max_concurrency: int = 2,
        isolated: bool = True,
        metrics: Metrics | None = None,
    ) -> None:
        self._runtime = agent_runtime
        self._registry = agent_registry
        self._max_concurrency = max(1, max_concurrency)
        self._isolated = isolated
        self._metrics = metrics or Metrics()

    async def run(
        self,
        graph: TaskGraph,
        *,
        session_id: str,
        cancellation: CancellationToken | None = None,
        trace_id: str | None = None,
    ) -> SchedulerResult:
        semaphore = asyncio.Semaphore(self._max_concurrency)
        write_lock = asyncio.Lock()
        completed: set[str] = set()
        failed: set[str] = set()
        workers: dict[str, WorkerRun] = {}
        running: dict[asyncio.Task[WorkerRun], str] = {}
        max_parallel = 0

        while True:
            busy = set(running.values())
            for step in graph.ready(completed, exclude=busy | set(workers)):
                task = asyncio.create_task(
                    self._run_step(
                        step, session_id, semaphore, write_lock, cancellation, trace_id
                    )
                )
                running[task] = step.id
            if not running:
                break
            max_parallel = max(max_parallel, len(running))
            done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                step_id = running.pop(task)
                worker = task.result()  # _run_step 恒不抛
                workers[step_id] = worker
                if worker.verification.ok and worker.result.ok:
                    completed.add(step_id)
                else:
                    failed.add(step_id)

        return SchedulerResult(
            workers=workers,
            completed=completed,
            failed=failed,
            blocked=graph.blocked_by(failed),
            max_parallel=max_parallel,
        )

    async def _run_step(
        self,
        step: Step,
        session_id: str,
        semaphore: asyncio.Semaphore,
        write_lock: asyncio.Lock,
        cancellation: CancellationToken | None,
        trace_id: str | None,
    ) -> WorkerRun:
        definition = self._registry.get(step.agent_id)
        async with semaphore:
            if not self._isolated and not step.read_only:
                async with write_lock:
                    return await self._runtime.run(
                        definition,
                        step,
                        session_id=session_id,
                        cancellation=cancellation,
                        trace_id=trace_id,
                    )
            return await self._runtime.run(
                definition,
                step,
                session_id=session_id,
                cancellation=cancellation,
                trace_id=trace_id,
            )
