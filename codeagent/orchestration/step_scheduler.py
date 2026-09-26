"""StepScheduler：推测执行 + 乐观集成 + 确定性串行集成（Phase 1 重构）。

原来的问题：所有 Worker 从同一旧 base 并行起步、末尾一次性大合并 —— DAG 后继在派发时
从旧 HEAD 切 worktree,**看不到前驱改动**,即使没有 git 冲突也在过期代码上工作。

现在：readiness 以 **integrated**（已并回 base）为准,不是本地验收。一个 Worker 验收通过后
进入集成队列;集成协调器**串行、按 (计划 step order) 确定序**逐个并回 base,成功才标记
integrated —— 只有此刻才解锁其 DAG 后继。后继被派发时 worktree 从含前驱改动的最新 HEAD
切出,不再过期。

并行不变（同层兄弟仍并发执行）;只有"集成"这一段串行且确定。仍用
asyncio.wait(FIRST_COMPLETED)（不用 TaskGroup,避免首个异常取消兄弟 → 孤立 tool_call）。

写串行回退：非隔离 workspace 下,非 read_only 的 Step 持写锁串行;read_only 免锁并行。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from codeagent.agent.registry import AgentRegistry
from codeagent.infra.cancellation import CancellationToken
from codeagent.infra.metrics import Metrics
from codeagent.orchestration.integration_coordinator import IntegrationCoordinator
from codeagent.orchestration.task_graph import Step, TaskGraph
from codeagent.runtime.agent_runtime import AgentRuntime, WorkerRun

# 集成后回调：记录 (step_id, worker, integrated)。integrated=False 表示执行/验收/集成任一失败。
StepCallback = Callable[[str, WorkerRun, bool], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    workers: dict[str, WorkerRun] = field(default_factory=dict)
    completed: set[str] = field(default_factory=set)  # 本地验收通过（未必已集成）
    integrated: set[str] = field(default_factory=set)  # 已并回 base（依赖调度以此为准）
    failed: set[str] = field(default_factory=set)  # 执行/验收/集成任一失败
    blocked: set[str] = field(default_factory=set)
    integrated_branches: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    max_parallel: int = 0


class StepScheduler:
    def __init__(
        self,
        *,
        agent_runtime: AgentRuntime,
        agent_registry: AgentRegistry,
        max_concurrency: int = 2,
        isolated: bool = True,
        integration_coordinator: IntegrationCoordinator | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._runtime = agent_runtime
        self._registry = agent_registry
        self._max_concurrency = max(1, max_concurrency)
        self._isolated = isolated
        self._coordinator = integration_coordinator or IntegrationCoordinator()
        self._metrics = metrics or Metrics()

    async def run(
        self,
        graph: TaskGraph,
        *,
        session_id: str,
        cancellation: CancellationToken | None = None,
        trace_id: str | None = None,
        skip: frozenset[str] | set[str] = frozenset(),
        on_step_complete: StepCallback | None = None,
    ) -> SchedulerResult:
        semaphore = asyncio.Semaphore(self._max_concurrency)
        write_lock = asyncio.Lock()
        step_order = {step.id: i for i, step in enumerate(graph.steps)}

        integrated: set[str] = set(skip)  # 恢复：已集成的 Step 视为满足依赖,不重跑
        completed: set[str] = set(skip)
        failed: set[str] = set()
        workers: dict[str, WorkerRun] = {}
        integrated_branches: list[str] = []
        conflicts: list[str] = []
        running: dict[asyncio.Task[WorkerRun], str] = {}
        pending: list[str] = []  # 已验收、待集成的 step_id（确定序消费）
        dispatched: set[str] = set(skip)
        max_parallel = 0

        async def _notify(step_id: str, worker: WorkerRun, ok: bool) -> None:
            if on_step_complete is not None:
                try:
                    await on_step_complete(step_id, worker, ok)
                except Exception:
                    self._metrics.incr("scheduler.checkpoint_failures")

        while True:
            # ① 派发：依赖已 integrated 且未派发的 Step
            for step in graph.ready(integrated, exclude=dispatched):
                dispatched.add(step.id)
                task = asyncio.create_task(
                    self._run_step(
                        step, session_id, semaphore, write_lock, cancellation, trace_id
                    )
                )
                running[task] = step.id
            max_parallel = max(max_parallel, len(running))

            # ② 串行集成：取 step order 最小的一个待集成 Worker 并回 base,然后回到①解锁后继
            if pending:
                pending.sort(key=lambda sid: step_order.get(sid, 1 << 30))
                step_id = pending.pop(0)
                worker = workers[step_id]
                outcome = await self._coordinator.integrate(worker)
                if outcome.integrated:
                    integrated.add(step_id)
                    if outcome.branch:
                        integrated_branches.append(outcome.branch)
                else:
                    failed.add(step_id)
                    if outcome.conflict:
                        conflicts.append(f"{outcome.branch}: {outcome.conflict}")
                await _notify(step_id, worker, outcome.integrated)
                continue

            # ③ 无在跑、无待集成 → 结束
            if not running:
                break

            # ④ 等一个 Worker 完成
            done, _ = await asyncio.wait(running.keys(), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                step_id = running.pop(task)
                worker = task.result()  # _run_step 恒不抛
                workers[step_id] = worker
                if worker.verification.ok and worker.result.ok:
                    completed.add(step_id)
                    pending.append(step_id)  # 进集成队列,由②串行处理
                else:
                    failed.add(step_id)
                    await _notify(step_id, worker, False)

        return SchedulerResult(
            workers=workers,
            completed=completed,
            integrated=integrated,
            failed=failed,
            blocked=graph.blocked_by(failed),
            integrated_branches=integrated_branches,
            conflicts=conflicts,
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
