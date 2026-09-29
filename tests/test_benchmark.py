"""Phase 9b/9d：benchmark 聚合纯函数单测（合成结果，不调真实 LLM）。"""

from __future__ import annotations

from scenarios.benchmark import _agg, summarize
from scenarios.runner import RunRecord


def _rec(name: str, *, ok: bool, attempts: int, integrated: bool, checks: bool) -> RunRecord:
    return RunRecord(
        name=name, ok=ok, integrated=integrated, attempts=attempts,
        checks_passed=checks, reruns=1, wall_s=10.0, in_tokens=100, out_tokens=50, llm_calls=3,
    )


def test_agg_rates_and_false_accept() -> None:
    items = [
        _rec("a", ok=True, attempts=1, integrated=True, checks=True),   # 成功 + 首次
        _rec("b", ok=True, attempts=2, integrated=True, checks=True),   # 成功 非首次
        _rec("c", ok=False, attempts=1, integrated=True, checks=False),  # 假接受（放行错产物）
        _rec("d", ok=False, attempts=2, integrated=False, checks=False),  # 未达成
    ]
    a = _agg(items)
    assert a["n"] == 4
    assert a["success"] == 2
    assert a["success_rate"] == 0.5
    assert a["first_success"] == 1
    assert a["first_success_rate"] == 0.25
    assert a["false_accept"] == 1  # 只有 c：integrated 但 checks 没过
    assert a["avg_tokens"] == 150.0
    assert a["avg_llm_calls"] == 3.0


def test_summarize_groups_by_dimension_and_tier() -> None:
    rows = [
        ("worker", "easy", _rec("a", ok=True, attempts=1, integrated=True, checks=True)),
        ("worker", "hard", _rec("b", ok=False, attempts=1, integrated=False, checks=False)),
        ("planner", "medium", _rec("c", ok=True, attempts=1, integrated=True, checks=True)),
    ]
    s = summarize(rows)
    assert s["overall"]["n"] == 3
    assert s["by_dimension"]["worker"]["n"] == 2
    assert s["by_dimension"]["worker"]["success_rate"] == 0.5
    assert s["by_dimension"]["planner"]["success_rate"] == 1.0
    assert set(s["by_tier"]) == {"easy", "hard", "medium"}


def test_agg_empty_is_safe() -> None:
    a = _agg([])
    assert a["n"] == 0
    assert a["success_rate"] == 0.0
