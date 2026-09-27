"""Scenario / Check 数据模型与判据构造器。

Check 是 `(repo: Path) -> CheckResult`：对跑完后 sandbox 的**真实工作树**做断言。
判据必须确定性、可自动判定（文件存在/内容/行集合/子串），不看 Agent 的自述。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from codeagent.orchestration.task_graph import TaskGraph

Check = Callable[[Path], "CheckResult"]


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    description: str
    task: str  # 传给 master.run 的目标（LLM planner 用；static graph 时仅作说明/日志）
    seed_files: Mapping[str, str] = field(default_factory=dict)
    # 提供则用 StaticPlanner 固定 DAG（只钉图，Worker 仍走真实 LLM）；None → 真实 LlmPlanner
    graph_factory: Callable[[], TaskGraph] | None = None
    checks: Sequence[Check] = ()
    expect_integrated: bool = True  # 期望是否成功 promote（verify_fail 类为 False）
    verify_command: str | None = None  # 覆盖 CODEAGENT_VERIFY_CMD（如 "false" 强制拒绝）
    profile_overrides: Mapping[str, object] = field(default_factory=dict)


def _read(repo: Path, rel: str) -> str | None:
    p = repo / rel
    return p.read_text(encoding="utf-8") if p.is_file() else None


def file_exists(rel: str) -> Check:
    def check(repo: Path) -> CheckResult:
        ok = (repo / rel).is_file()
        return CheckResult(ok, "" if ok else f"缺文件 {rel}")

    return check


def file_contains(rel: str, needle: str) -> Check:
    def check(repo: Path) -> CheckResult:
        text = _read(repo, rel)
        if text is None:
            return CheckResult(False, f"缺文件 {rel}")
        ok = needle in text
        return CheckResult(ok, "" if ok else f"{rel} 未包含 {needle!r}")

    return check


def file_line_multiset(rel: str, expected: Sequence[str]) -> Check:
    """非空行的多重集合相等（顺序无关，但重复次数必须一致 → 抓"副作用重复叠加"）。"""

    def check(repo: Path) -> CheckResult:
        text = _read(repo, rel)
        if text is None:
            return CheckResult(False, f"缺文件 {rel}")
        got = sorted(ln.strip() for ln in text.splitlines() if ln.strip())
        want = sorted(e.strip() for e in expected)
        ok = got == want
        return CheckResult(ok, "" if ok else f"{rel} 行={got} 期望={want}")

    return check
