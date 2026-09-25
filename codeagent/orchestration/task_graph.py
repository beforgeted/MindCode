"""TaskGraph：Worker Step 的依赖 DAG。

调度按 pending-set 增量派发（见 StepScheduler），这里只负责数据与就绪判定。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Step:
    id: str
    agent_id: str
    instruction: str
    dependencies: frozenset[str] = field(default_factory=frozenset)
    # read_only=True 的 Step 不写工作区，非隔离模式下仍可并行（免写锁）。
    read_only: bool = False


class TaskGraphError(ValueError):
    pass


class TaskGraph:
    def __init__(self, steps: list[Step] | tuple[Step, ...]) -> None:
        self._steps: dict[str, Step] = {}
        for step in steps:
            if step.id in self._steps:
                raise TaskGraphError(f"重复的 Step id: {step.id}")
            self._steps[step.id] = step
        self._validate()

    def _validate(self) -> None:
        for step in self._steps.values():
            for dep in step.dependencies:
                if dep not in self._steps:
                    raise TaskGraphError(f"Step {step.id} 依赖不存在的 {dep}")
                if dep == step.id:
                    raise TaskGraphError(f"Step {step.id} 依赖自身")
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        WHITE, GRAY, BLACK = 0, 1, 2
        color = dict.fromkeys(self._steps, WHITE)

        def visit(node: str) -> None:
            color[node] = GRAY
            for dep in self._steps[node].dependencies:
                if color[dep] == GRAY:
                    raise TaskGraphError(f"检测到依赖环，涉及 {node} -> {dep}")
                if color[dep] == WHITE:
                    visit(dep)
            color[node] = BLACK

        for node in self._steps:
            if color[node] == WHITE:
                visit(node)

    @property
    def steps(self) -> tuple[Step, ...]:
        return tuple(self._steps.values())

    def get(self, step_id: str) -> Step:
        return self._steps[step_id]

    def ready(self, completed: set[str], *, exclude: set[str] | None = None) -> list[Step]:
        """依赖全部完成、且未完成/未在排除集合里的 Step。"""
        skip = completed | (exclude or set())
        return [
            step
            for step_id, step in self._steps.items()
            if step_id not in skip and step.dependencies <= completed
        ]

    def blocked_by(self, failed: set[str]) -> set[str]:
        """因（直接或传递）依赖失败而无法运行的 Step。"""
        blocked: set[str] = set()
        changed = True
        while changed:
            changed = False
            for step_id, step in self._steps.items():
                if step_id in blocked:
                    continue
                if step.dependencies & (failed | blocked):
                    blocked.add(step_id)
                    changed = True
        return blocked
