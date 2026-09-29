"""第③层：真实 LlmPlanner 专项探针（planning quality）。

②的场景套件把"规划质量"变量隔离掉了（static graph），专测集成收敛。这一层反过来：
只喂任务给**真实 LlmPlanner**，反复采样它拆出的图，观察规划质量与稳定性——目前系统
最大的盲区（弱模型最容易把任务拆错，正是 finding 001 的根因：把小任务拆成两个都写
同一文件的步骤）。

只调 planner.plan()（每次一个 LLM 调用），不跑 Worker/git，便宜且聚焦。对每个任务采样
N 次，报告：步数分布、依赖边占比、并行宽度，以及**并发同文件写风险**（两个无依赖关系
的 Step 指令里出现同一文件名 → 计划层就制造了写写重叠）。

用法：
    python -m scenarios.planner_probe                # 全部任务，各采样 5 次
    python -m scenarios.planner_probe --samples 8
    python -m scenarios.planner_probe --only fizzbuzz_test
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from codeagent.cli.app import _build_client, _load_dotenv, _repair_ca_env
from codeagent.config import AppConfig
from codeagent.llm.types import ModelConfig
from codeagent.orchestration.planner import LlmPlanner
from codeagent.orchestration.task_graph import TaskGraph

_FILE_RE = re.compile(r"[\w./-]+\.[A-Za-z]{1,4}\b")


@dataclass(frozen=True, slots=True)
class PlannerCase:
    name: str
    task: str
    note: str
    expect_min_steps: int = 1
    expect_max_steps: int | None = None


CASES: list[PlannerCase] = [
    PlannerCase(
        name="fizzbuzz_test",
        task="创建 fizzbuzz.py 实现 fizzbuzz(n)，并为它写 pytest 单元测试",
        note="经典过度拆分陷阱（finding 001）：勿把'实现+测试'拆成两个都写 fizzbuzz.py 的步",
        expect_max_steps=2,
    ),
    PlannerCase(
        name="three_modules",
        task="创建 add.py、sub.py、mul.py 三个模块，各实现对应的二元运算函数",
        note="真并行：三文件互不相关，应拆成 3 个无依赖 Step（考察能否发现并行）",
        expect_min_steps=2,
    ),
    PlannerCase(
        name="add_field_propagate",
        task="给 user.py 里的 User 类加一个 email 字段，并更新所有引用 User 的地方",
        note="真依赖：改定义→改引用有先后，应有依赖边或收敛为单步，勿并列无依赖",
    ),
    PlannerCase(
        name="split_utils",
        task="重构 utils.py：把它拆成 string_utils.py 和 math_utils.py 两个文件",
        note="一拆二：原文件被删/改 + 两新文件，注意别造出都写 utils.py 的并发步",
    ),
    PlannerCase(
        name="cli_and_readme",
        task="给项目加一个 CLI 入口 main.py，并写 README.md 说明用法",
        note="弱依赖：README 通常应在 main.py 之后（描述其用法），考察是否串起来",
    ),
]


@dataclass(slots=True)
class Probe:
    name: str
    samples: int = 0
    step_counts: Counter[int] = field(default_factory=Counter)
    with_dep: int = 0  # 至少有一条依赖边的样本数
    concurrent_file_risk: int = 0  # 出现"无依赖关系的两步写同一文件名"的样本数
    bound_violations: int = 0  # 步数越界（过度拆分/退化）的样本数
    risk_files: Counter[str] = field(default_factory=Counter)


def _files_of(instruction: str) -> set[str]:
    return {m.group(0).lstrip("./") for m in _FILE_RE.finditer(instruction)}


def _dep_closure(graph: TaskGraph) -> dict[str, set[str]]:
    """每个 Step 的**传递**依赖闭包（fixpoint，同 TaskGraph.blocked_by 的思路）。"""
    closure = {s.id: set(s.dependencies) for s in graph.steps}
    changed = True
    while changed:
        changed = False
        for ds in closure.values():
            add: set[str] = set()
            for d in ds:
                add |= closure.get(d, set())
            if not add <= ds:
                ds |= add
                changed = True
    return closure


def _concurrent_same_file(graph: TaskGraph) -> set[str]:
    """返回被"两个互不依赖的 Step"同时提及的文件名（计划层制造的写写重叠风险）。

    有序判定用**传递闭包**：a→b→c 里 a 与 c 虽无直接边，但 c 传递依赖 a → 有先后、不算并发。
    只有任一方向都不可达的两步才算真正并发（消除 008 里"只看直接边"的假阳性）。
    """
    steps = list(graph.steps)
    closure = _dep_closure(graph)
    risky: set[str] = set()
    for i, a in enumerate(steps):
        fa = _files_of(a.instruction)
        for b in steps[i + 1 :]:
            # 任一方向（传递）可达 → 有先后，不算并发
            if b.id in closure.get(a.id, set()) or a.id in closure.get(b.id, set()):
                continue
            risky |= fa & _files_of(b.instruction)
    return risky


def _analyze(case: PlannerCase, graph: TaskGraph, probe: Probe) -> None:
    n = len(graph.steps)
    probe.samples += 1
    probe.step_counts[n] += 1
    if any(s.dependencies for s in graph.steps):
        probe.with_dep += 1
    risky = _concurrent_same_file(graph)
    if risky:
        probe.concurrent_file_risk += 1
        probe.risk_files.update(risky)
    lo, hi = case.expect_min_steps, case.expect_max_steps
    if n < lo or (hi is not None and n > hi):
        probe.bound_violations += 1


def _dist(counter: Counter[int]) -> str:
    return ", ".join(f"{k}步×{v}" for k, v in sorted(counter.items()))


async def _run(only: list[str], samples: int) -> int:
    cases = [c for c in CASES if not only or c.name in only]
    if not cases:
        print(f"没有匹配的任务。可用：{', '.join(c.name for c in CASES)}")
        return 2
    config = AppConfig.from_env(Path.cwd())
    client = _build_client(config)
    model_config = ModelConfig(model=config.model, context_window=config.profile.context_window)
    planner = LlmPlanner(client, model_config)

    probes: list[Probe] = []
    for case in cases:
        probe = Probe(name=case.name)
        print(f"\n=== {case.name} × {samples}\n    任务：{case.task}\n    关注：{case.note}")
        for _ in range(samples):
            graph = await planner.plan(case.task)
            _analyze(case, graph, probe)
        probes.append(probe)
        print(f"    步数分布：{_dist(probe.step_counts)}")

    print(f"\n{'任务':<20}{'样本':<5}{'步数分布':<22}{'含依赖':<7}{'并发同文件险':<13}{'越界':<5}")
    print("-" * 74)
    warn = 0
    for p in probes:
        flagged = p.concurrent_file_risk > 0 or p.bound_violations > 0
        warn += 1 if flagged else 0
        print(
            f"{p.name:<20}{p.samples:<5}{_dist(p.step_counts):<22}"
            f"{f'{p.with_dep}/{p.samples}':<7}"
            f"{f'{p.concurrent_file_risk}/{p.samples}':<13}"
            f"{f'{p.bound_violations}/{p.samples}':<5}"
        )
    for p in probes:
        if p.risk_files:
            top = ", ".join(f"{f}×{n}" for f, n in p.risk_files.most_common(5))
            print(f"\n[{p.name}] 并发同文件风险涉及：{top}")
    print(f"\n{len(probes)} 个任务，{warn} 个出现规划风险（并发同文件写 / 步数越界）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="scenarios.planner_probe")
    parser.add_argument("--only", nargs="*", default=[], help="只探测指定任务名")
    parser.add_argument("--samples", type=int, default=5, help="每个任务采样次数")
    parser.add_argument("--env", type=Path, default=None, help=".env 路径")
    args = parser.parse_args()
    loaded = _load_dotenv(args.env, Path.cwd() / ".env")
    if loaded is not None:
        print(f"[已加载 {loaded}]")
    _repair_ca_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[警告] 未配置 ANTHROPIC_API_KEY：Planner 走 Stub，退化单步，探测无意义。")
    return asyncio.run(_run(args.only, max(1, args.samples)))


if __name__ == "__main__":
    raise SystemExit(main())

