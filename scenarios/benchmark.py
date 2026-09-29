"""Phase 9：小规模质量 benchmark —— 在场景套件基建上加难度/维度分层与聚合指标。

回答的问题从"收敛机制有没有退化"（场景套件②）升级到"整体能力多强、代价多大"：
成功率 / 首次成功率 / 重试·兜底用量 / 每任务 token·耗时 / 验证器假接受。复用 runner 的隔离
sandbox 与打分，只在其上加**观测聚合**，不碰运行时逻辑。

opt-in：`python -m scenarios.benchmark [--repeat N] [--only ...] [--dimension ...]`。
普通 pytest 不触发（本模块无 test_ 前缀、不被收集）。判据哲学同 007：正确性用断言，能力/成本用观察。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from codeagent.cli.app import _load_dotenv, _repair_ca_env
from codeagent.orchestration.task_graph import Step, TaskGraph
from scenarios.model import Scenario, file_contains, file_exists, file_line_multiset
from scenarios.runner import RunRecord, _run_once
from scenarios.suite import SCENARIOS


@dataclass(frozen=True, slots=True)
class BenchTask:
    scenario: Scenario
    tier: str  # easy / medium / hard
    dimension: str  # planner / worker / verifier / integration


# 现有 11 场景按 维度×难度 归类（复用其判据与 ground-truth）
_TAGS: dict[str, tuple[str, str]] = {
    "simple_create": ("worker", "easy"),
    "dep_chain": ("integration", "easy"),
    "deep_chain": ("integration", "medium"),
    "overlap_append": ("integration", "medium"),
    "wide_fanout": ("integration", "hard"),
    "strong_conflict": ("integration", "hard"),
    "verify_pass": ("verifier", "easy"),
    "verify_fail": ("verifier", "easy"),
    "planner_freeform": ("planner", "medium"),
    "planner_trap": ("planner", "medium"),
    "split_utils_e2e": ("planner", "hard"),
}


def _tasks() -> list[BenchTask]:
    tasks = [
        BenchTask(s, tier=_TAGS.get(s.name, ("worker", "medium"))[1],
                  dimension=_TAGS.get(s.name, ("worker", "medium"))[0])
        for s in SCENARIOS
    ]
    tasks.extend(_extra_tasks())
    return tasks


def _single(instruction: str) -> TaskGraph:
    return TaskGraph([Step(id="s", agent_id="default", instruction=instruction)])


def _extra_tasks() -> list[BenchTask]:
    """在 11 场景之外补充的任务，铺开 worker/planner/verifier/integration × 难度。"""
    out: list[BenchTask] = []

    # worker：明确指令、固定图，隔离规划变量
    out.append(BenchTask(Scenario(
        name="w_bugfix", description="修 add 的符号 bug", task="把 calc.py 里 add 的减号改成加号",
        seed_files={"calc.py": "def add(a, b):\n    return a - b\n"},
        graph_factory=lambda: _single("把 calc.py 里 add 函数的 `a - b` 改成 `a + b`，其它不动"),
        checks=[file_contains("calc.py", "a + b")],
    ), tier="easy", dimension="worker"))
    out.append(BenchTask(Scenario(
        name="w_rename", description="重命名函数", task="把 foo 改名为 bar",
        seed_files={"m.py": "def foo(x):\n    return x * 2\n"},
        graph_factory=lambda: _single("把 m.py 里的函数 foo 重命名为 bar，函数体不变"),
        checks=[file_contains("m.py", "def bar")],
    ), tier="easy", dimension="worker"))
    out.append(BenchTask(Scenario(
        name="w_two_files", description="两独立文件（固定图并行）",
        task="创建 add.py 与 sub.py",
        graph_factory=lambda: TaskGraph([
            Step("a", "default", "创建 add.py，实现 add(a,b) 返回 a+b"),
            Step("b", "default", "创建 sub.py，实现 sub(a,b) 返回 a-b"),
        ]),
        checks=[file_exists("add.py"), file_exists("sub.py")],
    ), tier="medium", dimension="worker"))

    # planner：自然语言目标，考察真实拆图 + 运行时收敛
    out.append(BenchTask(Scenario(
        name="p_three_consts", description="三独立文件（真并行）",
        task="创建 x.py、y.py、z.py，分别定义常量 X=1、Y=2、Z=3",
        checks=[file_contains("x.py", "X"), file_contains("y.py", "Y"), file_contains("z.py", "Z")],
    ), tier="medium", dimension="planner"))
    out.append(BenchTask(Scenario(
        name="p_pipeline", description="自然语言依赖链",
        task="先创建 conf.py 定义 N=3；再创建 gen.py，读取 conf.py 的 N，写 out.txt 内容为该数字",
        checks=[file_contains("conf.py", "N"), file_line_multiset("out.txt", ["3"])],
    ), tier="hard", dimension="planner"))

    # verifier：正例（确定性命令应通过）
    out.append(BenchTask(Scenario(
        name="v_pass_grep", description="确定性验收正例",
        task="创建 flag.txt，内容恰好是 READY",
        graph_factory=lambda: _single("创建 flag.txt，内容恰好是 READY（无多余字符）"),
        checks=[file_line_multiset("flag.txt", ["READY"])],
        verify_command="grep -qx READY flag.txt",
    ), tier="easy", dimension="verifier"))

    # integration：更深链、更宽扇出
    out.append(BenchTask(Scenario(
        name="i_deep4", description="四层依赖链",
        task="v1=1 → v2=2 → v3=3 → v4=4",
        graph_factory=_deep4,
        checks=[file_line_multiset("v4.txt", ["4"])],
    ), tier="hard", dimension="integration"))
    out.append(BenchTask(Scenario(
        name="i_fanout4", description="四路并行追加",
        task="四步各向 log.txt 追加一行",
        graph_factory=lambda: _fanout("log.txt", ("w", "x", "y", "z")),
        checks=[file_line_multiset("log.txt", ["w", "x", "y", "z"])],
    ), tier="hard", dimension="integration"))
    return out


def _deep4() -> TaskGraph:
    steps = [Step("a", "default", "创建 v1.txt 内容为数字 1（只此一个数字）")]
    prev, vals = "a", [("b", "v1.txt", "v2.txt", 2), ("c", "v2.txt", "v3.txt", 3),
                       ("d", "v3.txt", "v4.txt", 4)]
    for sid, src, dst, val in vals:
        steps.append(Step(
            sid, "default",
            f"先 read_file 读 {src} 的数字，创建 {dst}，"
            f"内容是该数字+1（这里是 {val}），只写这个数字",
            dependencies=frozenset({prev}),
        ))
        prev = sid
    return TaskGraph(steps)


def _fanout(target: str, tags: tuple[str, ...]) -> TaskGraph:
    return TaskGraph([
        Step(t, "default", f"向仓库根的 {target} 追加一行 {t}（用 >> 追加，不存在则创建）")
        for t in tags
    ])


def _mean(xs: list[float]) -> float:
    return round(sum(xs) / len(xs), 1) if xs else 0.0


def _agg(items: list[RunRecord]) -> dict:
    """一组任务结果的聚合指标（纯函数，便于单测）。"""
    n = len(items)
    ok = sum(1 for r in items if r.ok)
    first = sum(1 for r in items if r.ok and r.attempts == 1)
    # 假接受：verifier 放行并 promote（integrated），但产物断言没过 → 错产物被当成对的。
    false_accept = sum(1 for r in items if r.integrated and not r.checks_passed)
    return {
        "n": n,
        "success": ok,
        "success_rate": round(ok / n, 3) if n else 0.0,
        "first_success": first,
        "first_success_rate": round(first / n, 3) if n else 0.0,
        "false_accept": false_accept,
        "avg_attempts": _mean([r.attempts for r in items]),
        "avg_reruns": _mean([float(r.reruns) for r in items]),
        "avg_integrations": _mean([float(r.integrations) for r in items]),
        "avg_wall_s": _mean([r.wall_s for r in items]),
        "avg_llm_calls": _mean([float(r.llm_calls) for r in items]),
        "avg_tokens": _mean([float(r.in_tokens + r.out_tokens) for r in items]),
    }


def summarize(rows: list[tuple[str, str, RunRecord]]) -> dict:
    """rows = (dimension, tier, RunRecord)。产出 overall + 分维度 + 分难度 聚合。"""
    by_dim: dict[str, list[RunRecord]] = defaultdict(list)
    by_tier: dict[str, list[RunRecord]] = defaultdict(list)
    for dim, tier, rec in rows:
        by_dim[dim].append(rec)
        by_tier[tier].append(rec)
    return {
        "overall": _agg([r for _, _, r in rows]),
        "by_dimension": {d: _agg(v) for d, v in sorted(by_dim.items())},
        "by_tier": {t: _agg(v) for t, v in sorted(by_tier.items())},
    }


def _print_group(title: str, groups: dict[str, dict]) -> None:
    print(f"\n{title}")
    header = (
        f"{'组':<14}{'n':<4}{'成功率':<8}{'首次':<8}{'假接受':<7}"
        f"{'均重跑':<7}{'均耗时s':<8}{'均tok':<8}"
    )
    print(header)
    print("-" * len(header))
    for name, a in groups.items():
        print(
            f"{name:<14}{a['n']:<4}{a['success_rate']:<8}{a['first_success_rate']:<8}"
            f"{a['false_accept']:<7}{a['avg_reruns']:<7}{a['avg_wall_s']:<8}{a['avg_tokens']:<8}"
        )


async def _main(only: list[str], dims: list[str], repeat: int) -> int:
    tasks = _tasks()
    if only:
        tasks = [t for t in tasks if t.scenario.name in only]
    if dims:
        tasks = [t for t in tasks if t.dimension in dims]
    if not tasks:
        print("没有匹配的任务。")
        return 2
    rows: list[tuple[str, str, RunRecord]] = []
    for task in tasks:
        for i in range(repeat):
            rec = await _run_once(task.scenario, keep=False)
            rows.append((task.dimension, task.tier, rec))
            tag = "PASS" if rec.ok else "FAIL"
            print(f"[{task.dimension}/{task.tier}] {task.scenario.name} "
                  f"[{i + 1}/{repeat}] {tag} ({rec.wall_s}s)")
    summary = summarize(rows)
    _print_group("== 总体 ==", {"overall": summary["overall"]})
    _print_group("== 分维度 ==", summary["by_dimension"])
    _print_group("== 分难度 ==", summary["by_tier"])
    report = Path(__file__).with_name("last_benchmark.json")
    report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {report}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="scenarios.benchmark")
    parser.add_argument("--only", nargs="*", default=[], help="只跑指定任务名")
    parser.add_argument("--dimension", nargs="*", default=[],
                        help="只跑指定维度：planner/worker/verifier/integration")
    parser.add_argument("--repeat", type=int, default=1, help="每个任务跑几次（看稳定性）")
    parser.add_argument("--env", type=Path, default=None, help=".env 路径")
    args = parser.parse_args()
    loaded = _load_dotenv(args.env, Path.cwd() / ".env")
    if loaded is not None:
        print(f"[已加载 {loaded}]")
    _repair_ca_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[警告] 未配置 ANTHROPIC_API_KEY：走 Stub，benchmark 无意义。")
    return asyncio.run(_main(args.only, args.dimension, max(1, args.repeat)))


if __name__ == "__main__":
    raise SystemExit(main())

