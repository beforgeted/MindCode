"""Scenario runner：隔离 sandbox → 跑真实 MasterRuntime → 对真实 base 打分 → 汇总指标。

每个场景一个全新的临时 git 仓库（sandbox），CODEAGENT_HOME 也指到 sandbox 内，
彼此不污染。场景串行执行（避免 CODEAGENT_HOME 全局环境变量互相覆盖）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from codeagent.cli.app import _build_client, _load_dotenv, _repair_ca_env
from codeagent.config import AppConfig
from codeagent.orchestration.master_session import MasterSession
from codeagent.orchestration.planner import StaticPlanner
from scenarios.model import Scenario
from scenarios.suite import SCENARIOS


def _git(cwd: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


def _seed_sandbox(scenario: Scenario) -> tuple[Path, str]:
    """建临时 git 仓库、写种子文件、初始提交，返回 (repo_path, initial_head)。"""
    repo = Path(tempfile.mkdtemp(prefix=f"mc_scn_{scenario.name}_"))
    for rel, content in scenario.seed_files.items():
        dst = repo / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
    if not scenario.seed_files:
        (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "bench@mindcode.local")
    _git(repo, "config", "user.name", "mindcode-bench")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "seed")
    return repo, _git(repo, "rev-parse", "HEAD")


@dataclass(slots=True)
class RunRecord:
    name: str
    ok: bool = False
    integrated: bool = False
    expect_integrated: bool = True
    attempts: int = 0
    reruns: int = 0
    stale: int = 0
    integrations: int = 0
    conflicts: int = 0
    steps_integrated: int = 0
    steps_failed: int = 0
    max_parallel: int = 0
    wall_s: float = 0.0
    base_moved: bool = False
    checks_passed: bool = False  # 仅 scenario.checks（产物断言）是否全过，独立于 integrated
    llm_calls: int = 0
    in_tokens: int = 0
    out_tokens: int = 0
    failures: list[str] = field(default_factory=list)  # 失败判据/原因，供排查


def _counter(snap: dict[str, dict[str, float]], key: str) -> int:
    return int(snap.get("counters", {}).get(key, 0))


async def _run_once(scenario: Scenario, *, keep: bool) -> RunRecord:
    rec = RunRecord(name=scenario.name, expect_integrated=scenario.expect_integrated)
    repo, initial_head = _seed_sandbox(scenario)
    os.environ["CODEAGENT_HOME"] = str(repo / ".state")
    config = AppConfig.from_env(repo)
    if scenario.profile_overrides:
        config = replace(config, profile=replace(config.profile, **scenario.profile_overrides))
    if scenario.verify_command is not None:
        config = replace(config, verify_command=scenario.verify_command)

    planner = StaticPlanner(scenario.graph_factory()) if scenario.graph_factory else None
    client = _build_client(config)
    started = time.perf_counter()
    try:
        async with MasterSession(config, llm_client=client, planner=planner) as ms:
            final = await ms.run_task(scenario.task)
            snap = ms.session.metrics.snapshot()
    finally:
        rec.wall_s = round(time.perf_counter() - started, 1)

    rec.integrated = final.integrated
    rec.attempts = final.replans + 1
    rec.reruns = _counter(snap, "scheduler.reruns")
    rec.stale = _counter(snap, "integration.stale")
    rec.integrations = _counter(snap, "scheduler.integrations")
    rec.conflicts = _counter(snap, "integration.conflicts")
    rec.llm_calls = _counter(snap, "llm.calls")
    rec.in_tokens = _counter(snap, "llm.input_tokens")
    rec.out_tokens = _counter(snap, "llm.output_tokens")
    sched = final.scheduler
    if sched is not None:
        rec.steps_integrated = len(sched.integrated)
        rec.steps_failed = len(sched.failed)
        rec.max_parallel = sched.max_parallel

    current_head = _git(repo, "rev-parse", "HEAD")
    rec.base_moved = current_head != initial_head
    _score(scenario, repo, rec, initial_head, current_head)

    if keep or not rec.ok:
        print(f"    sandbox 保留：{repo}")
    else:
        shutil.rmtree(repo, ignore_errors=True)
    return rec


def _score(
    scenario: Scenario, repo: Path, rec: RunRecord, initial_head: str, current_head: str
) -> None:
    """确定性打分：期望的 integrated 语义 + base 一致性 + 各判据。"""
    if rec.integrated != scenario.expect_integrated:
        rec.failures.append(f"integrated={rec.integrated} 期望={scenario.expect_integrated}")
    # 期望失败的场景：真实 base 必须分毫不动（事务丢弃的核心不变式）。
    if not scenario.expect_integrated and current_head != initial_head:
        rec.failures.append("期望不推进但 base HEAD 已变")
    # 期望成功的场景：base 应确实前进了（promote 发生过）。
    if scenario.expect_integrated and current_head == initial_head:
        rec.failures.append("期望完成但 base 未推进")
    checks_ok = True
    for check in scenario.checks:
        result = check(repo)
        if not result.ok:
            checks_ok = False
            rec.failures.append(result.detail)
    rec.checks_passed = checks_ok
    rec.ok = not rec.failures


def _print_table(records: list[RunRecord]) -> None:
    header = (
        f"{'场景':<18}{'结果':<6}{'集成':<6}{'尝试':<5}{'重跑':<5}"
        f"{'过期':<5}{'兜底':<5}{'冲突':<5}{'步(集/败)':<10}{'并行':<5}{'耗时s':<7}"
    )
    print("\n" + header)
    print("-" * len(header))
    for r in records:
        mark = "PASS" if r.ok else "FAIL"
        steps = f"{r.steps_integrated}/{r.steps_failed}"
        print(
            f"{r.name:<18}{mark:<6}{r.integrated!s:<6}{r.attempts:<5}{r.reruns:<5}"
            f"{r.stale:<5}{r.integrations:<5}{r.conflicts:<5}{steps:<10}"
            f"{r.max_parallel:<5}{r.wall_s:<7}"
        )
    for r in records:
        if r.failures:
            print(f"\n[{r.name}] 失败原因：")
            for f in r.failures:
                print(f"  - {f}")


async def _main(only: list[str], repeat: int, keep: bool) -> int:
    scenarios = [s for s in SCENARIOS if not only or s.name in only]
    if not scenarios:
        print(f"没有匹配的场景。可用：{', '.join(s.name for s in SCENARIOS)}")
        return 2
    all_records: list[RunRecord] = []
    for scenario in scenarios:
        print(f"\n=== {scenario.name} × {repeat} — {scenario.description}")
        for i in range(repeat):
            rec = await _run_once(scenario, keep=keep)
            tag = "PASS" if rec.ok else "FAIL"
            print(f"  [{i + 1}/{repeat}] {tag}  ({rec.wall_s}s)")
            all_records.append(rec)
    _print_table(all_records)
    passed = sum(1 for r in all_records if r.ok)
    print(f"\n总计 {passed}/{len(all_records)} 通过")
    report = Path(__file__).with_name("last_report.json")
    report.write_text(
        json.dumps([asdict(r) for r in all_records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"明细已写入 {report}")
    return 0 if passed == len(all_records) else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="scenarios.runner")
    parser.add_argument("--only", nargs="*", default=[], help="只跑指定场景名")
    parser.add_argument("--repeat", type=int, default=1, help="每个场景跑几次（看稳定性）")
    parser.add_argument("--keep", action="store_true", help="保留 sandbox 供排查")
    parser.add_argument("--env", type=Path, default=None, help=".env 路径")
    args = parser.parse_args()
    loaded = _load_dotenv(args.env, Path.cwd() / ".env")
    if loaded is not None:
        print(f"[已加载 {loaded}]")
    _repair_ca_env()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[警告] 未配置 ANTHROPIC_API_KEY：将走 StubLlmClient，Worker 不会真的写文件，"
              "场景基本会 FAIL。真实测试请先配 .env。")
    return asyncio.run(_main(args.only, max(1, args.repeat), args.keep))


if __name__ == "__main__":
    raise SystemExit(main())
